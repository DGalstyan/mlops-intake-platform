"""Generator determinism, snapshot stability, and the leakage assertion.

The leakage tests matter most. Training on the golden set makes every downstream
number look *better*, so no dashboard, alarm or gate in the rest of the system
would catch it — these tests are the only thing standing between that mistake and
a set of metrics that silently mean nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import DOCUMENT_CLASSES
from src.data import generate


class TestDeterminism:
    def test_same_seed_produces_identical_documents(self) -> None:
        first = generate.generate_documents(docs_per_class=5, seed=123)
        second = generate.generate_documents(docs_per_class=5, seed=123)
        assert first == second

    def test_different_seed_produces_different_text(self) -> None:
        first = generate.generate_documents(docs_per_class=5, seed=123)
        second = generate.generate_documents(docs_per_class=5, seed=456)
        assert [d.text for d in first] != [d.text for d in second]

    def test_determinism_survives_a_fresh_interpreter(self) -> None:
        """The important one: determinism must not depend on PYTHONHASHSEED.

        Python randomises str/bytes hashing per process. A seed derived from
        `hash((seed, "label", i))` would produce different data in every new
        interpreter while looking perfectly deterministic inside a single test
        session. Two subprocesses with hash randomisation explicitly enabled are
        the only way to catch that.
        """
        script = (
            "import json,sys;"
            "sys.path.insert(0, '.');"
            "from src.data import generate;"
            "docs = generate.generate_documents(docs_per_class=3, seed=99);"
            "print(json.dumps([d.text for d in docs]))"
        )
        repo_root = Path(__file__).resolve().parents[1]

        outputs = []
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=repo_root,
                env={"PYTHONHASHSEED": "random", "PATH": "/usr/bin:/bin"},
                check=True,
            )
            outputs.append(result.stdout.strip())

        assert outputs[0] == outputs[1], (
            "generation differs between interpreters — a seed is probably derived "
            "from hash() of a string, which Python randomises per process"
        )

    def test_class_counts_are_independent(self) -> None:
        """Changing the count for the corpus must not reshuffle earlier documents.

        Each class draws from its own RNG stream, so document N of a class is the
        same whether 5 or 50 were requested. Without this, regenerating a larger
        corpus would invalidate every previously-recorded snapshot id for no real
        reason.
        """
        small = generate.generate_documents(docs_per_class=3, seed=7)
        large = generate.generate_documents(docs_per_class=6, seed=7)
        small_by_id = {d.doc_id: d.text for d in small}
        large_by_id = {d.doc_id: d.text for d in large}
        for doc_id, text in small_by_id.items():
            assert large_by_id[doc_id] == text


class TestGeneratedCorpus:
    def test_every_class_is_represented(self) -> None:
        docs = generate.generate_documents(docs_per_class=4, seed=1)
        assert {d.label for d in docs} == set(DOCUMENT_CLASSES)

    def test_counts_are_balanced(self) -> None:
        docs = generate.generate_documents(docs_per_class=4, seed=1)
        for label in DOCUMENT_CLASSES:
            assert sum(1 for d in docs if d.label == label) == 4

    def test_doc_ids_are_unique(self) -> None:
        docs = generate.generate_documents(docs_per_class=10, seed=1)
        assert len({d.doc_id for d in docs}) == len(docs)

    def test_classes_share_vocabulary(self) -> None:
        """Classes must overlap, or the task is trivially separable.

        A generator whose classes share no words yields a 1.00 F1 and a
        degenerate confidence distribution, which would make the calibration
        metric and the Route threshold meaningless.
        """
        docs = generate.generate_documents(docs_per_class=30, seed=1)
        vocab: dict[str, set[str]] = {label: set() for label in DOCUMENT_CLASSES}
        for doc in docs:
            vocab[doc.label].update(doc.text.split())
        assert vocab["invoice"] & vocab["correspondence"]

    def test_header_carries_no_class_information(self) -> None:
        """The header must be identical in form across every class.

        An earlier version put the class name in each document's header, so the
        classifier could read the label straight off the text: macro-F1 1.00,
        ECE 0.0003, every confidence pinned at 1.0. That silently guts four
        later milestones — calibration has nothing to measure, the Route state's
        threshold can never fire, the review queue stays empty, and the drift
        demo has no headroom to move.
        """
        docs = generate.generate_documents(docs_per_class=25, seed=2)
        for doc in docs:
            first, second = doc.text.split()[:2]
            assert first == "document"
            assert second.isdigit()

    def test_no_single_token_perfectly_identifies_a_class(self) -> None:
        """No token may be a deterministic giveaway.

        This is the precise property that matters, and it is weaker than "the
        class name never appears" — the word "invoice" legitimately occurs in
        invoice documents and is real signal. What must not exist is a token
        present in *every* document of one class and *no* document of any other,
        because that is a perfect classification rule and makes the confidence
        distribution degenerate.
        """
        docs = generate.generate_documents(docs_per_class=30, seed=2)
        by_class: dict[str, list[set[str]]] = {
            label: [] for label in DOCUMENT_CLASSES
        }
        for doc in docs:
            by_class[doc.label].append(set(doc.text.split()[2:]))  # skip header

        for label in DOCUMENT_CLASSES:
            own_docs = by_class[label]
            in_every_own = set.intersection(*own_docs) if own_docs else set()
            in_any_other = {
                token
                for other, doc_sets in by_class.items()
                if other != label
                for tokens in doc_sets
                for token in tokens
            }
            giveaways = in_every_own - in_any_other
            assert not giveaways, (
                f"{label} is perfectly identified by {sorted(giveaways)} — the "
                "task is trivially separable and confidence will be degenerate"
            )

    def test_task_is_hard_enough_to_be_worth_calibrating(self) -> None:
        """Classes must overlap enough that confidence is not degenerate.

        Asserted on the data rather than on a trained model so the test stays
        fast: every class must draw a substantial share of its terms from pools
        it does not own. If this ratio drifts up, the classifier becomes perfect
        and the confidence gate becomes decorative.
        """
        docs = generate.generate_documents(docs_per_class=40, seed=2)
        own_terms = {
            label: set(generate._CLASS_TERMS[label]) for label in DOCUMENT_CLASSES
        }
        for label in DOCUMENT_CLASSES:
            texts = [d.text for d in docs if d.label == label]
            words = [w for t in texts for w in t.split()[2:]]  # skip the header
            own_fraction = sum(1 for w in words if w in own_terms[label]) / len(words)
            assert 0.2 < own_fraction < 0.75, (
                f"{label}: {own_fraction:.2%} of terms are own-class. Outside "
                "this band the task is either trivially separable or unlearnable."
            )

    def test_rejects_nonsense_count(self) -> None:
        with pytest.raises(ValueError, match="docs_per_class"):
            generate.generate_documents(docs_per_class=0)


class TestShiftedBatch:
    def test_shift_changes_vocabulary(self) -> None:
        normal = generate.generate_documents(docs_per_class=20, seed=5)
        shifted = generate.generate_documents(docs_per_class=20, seed=5, shifted=True)

        normal_vocab = {w for d in normal for w in d.text.split()}
        shifted_vocab = {w for d in shifted for w in d.text.split()}
        assert shifted_vocab - normal_vocab, "shifted batch introduced no new terms"

    def test_shift_changes_length_distribution(self) -> None:
        normal = generate.generate_documents(docs_per_class=20, seed=5)
        shifted = generate.generate_documents(docs_per_class=20, seed=5, shifted=True)

        normal_mean = sum(len(d.text) for d in normal) / len(normal)
        shifted_mean = sum(len(d.text) for d in shifted) / len(shifted)
        assert shifted_mean > normal_mean

    def test_shift_preserves_labels(self) -> None:
        """Input drift, not concept drift.

        The shifted batch must keep the same class balance — that is what makes
        it usable to demonstrate telling "the data changed" apart from "the model
        got worse".
        """
        shifted = generate.generate_documents(docs_per_class=8, seed=5, shifted=True)
        for label in DOCUMENT_CLASSES:
            assert sum(1 for d in shifted if d.label == label) == 8


class TestSnapshotId:
    def test_is_stable_for_identical_data(self) -> None:
        docs = generate.generate_documents(docs_per_class=4, seed=11)
        kwargs = {"seed": 11, "docs_per_class": 4, "golden_per_class": 1}
        assert generate.compute_snapshot_id(docs, **kwargs) == (
            generate.compute_snapshot_id(docs, **kwargs)
        )

    def test_changes_when_a_document_changes(self) -> None:
        docs = generate.generate_documents(docs_per_class=4, seed=11)
        kwargs = {"seed": 11, "docs_per_class": 4, "golden_per_class": 1}
        original = generate.compute_snapshot_id(docs, **kwargs)

        mutated = list(docs)
        mutated[0] = generate.Document(
            doc_id=mutated[0].doc_id, text="tampered", label=mutated[0].label
        )
        assert generate.compute_snapshot_id(mutated, **kwargs) != original

    def test_changes_when_generation_parameters_change(self) -> None:
        """Parameters are part of the identity, not just content.

        Two corpora could coincidentally contain the same documents while having
        been generated with a different golden split, which would make the id a
        misleading lineage key.
        """
        docs = generate.generate_documents(docs_per_class=4, seed=11)
        a = generate.compute_snapshot_id(
            docs, seed=11, docs_per_class=4, golden_per_class=1
        )
        b = generate.compute_snapshot_id(
            docs, seed=11, docs_per_class=4, golden_per_class=2
        )
        assert a != b

    def test_is_prefixed_with_its_algorithm(self) -> None:
        docs = generate.generate_documents(docs_per_class=2, seed=1)
        snapshot = generate.compute_snapshot_id(
            docs, seed=1, docs_per_class=2, golden_per_class=1
        )
        assert snapshot.startswith("sha256:")


class TestSplit:
    def test_golden_is_stratified(self) -> None:
        docs = generate.generate_documents(docs_per_class=20, seed=3)
        _, golden = generate.split_train_golden(docs, golden_per_class=5, seed=3)
        for label in DOCUMENT_CLASSES:
            assert sum(1 for d in golden if d.label == label) == 5

    def test_split_is_exhaustive_and_non_overlapping(self) -> None:
        docs = generate.generate_documents(docs_per_class=20, seed=3)
        train, golden = generate.split_train_golden(docs, golden_per_class=5, seed=3)
        assert len(train) + len(golden) == len(docs)
        assert not ({d.doc_id for d in train} & {d.doc_id for d in golden})

    def test_split_is_deterministic(self) -> None:
        docs = generate.generate_documents(docs_per_class=20, seed=3)
        first = generate.split_train_golden(docs, golden_per_class=5, seed=3)
        second = generate.split_train_golden(docs, golden_per_class=5, seed=3)
        assert [d.doc_id for d in first[1]] == [d.doc_id for d in second[1]]

    def test_golden_is_not_just_the_tail_of_the_generated_order(self) -> None:
        """The split shuffles rather than slicing.

        Slicing the generated order would make the golden set systematically the
        highest-numbered documents, correlating held-out data with generation
        order — a subtle sampling bias in the metric everything gates on.
        """
        docs = generate.generate_documents(docs_per_class=20, seed=3)
        _, golden = generate.split_train_golden(docs, golden_per_class=5, seed=3)
        invoice_indices = sorted(
            int(d.doc_id.rsplit("-", 1)[1]) for d in golden if d.label == "invoice"
        )
        assert invoice_indices != [15, 16, 17, 18, 19]

    def test_refuses_to_leave_nothing_to_train_on(self) -> None:
        docs = generate.generate_documents(docs_per_class=3, seed=3)
        with pytest.raises(ValueError, match="nothing would remain"):
            generate.split_train_golden(docs, golden_per_class=3, seed=3)


class TestLeakageAssertion:
    def test_passes_on_a_clean_split(self) -> None:
        docs = generate.generate_documents(docs_per_class=10, seed=3)
        train, golden = generate.split_train_golden(docs, golden_per_class=3, seed=3)
        generate.assert_disjoint(train, golden)  # must not raise

    def test_catches_a_shared_document_id(self) -> None:
        docs = generate.generate_documents(docs_per_class=10, seed=3)
        train, golden = generate.split_train_golden(docs, golden_per_class=3, seed=3)
        with pytest.raises(AssertionError, match="both train and golden"):
            generate.assert_disjoint(train + [golden[0]], golden)

    def test_catches_duplicated_text_under_a_different_id(self) -> None:
        """An id-only check is not enough.

        The same document copied under a new id is still leakage, and it is the
        more likely accident — e.g. a document delivered twice by an upstream
        system and assigned two ids.
        """
        docs = generate.generate_documents(docs_per_class=10, seed=3)
        train, golden = generate.split_train_golden(docs, golden_per_class=3, seed=3)
        disguised = generate.Document(
            doc_id="totally-different-id",
            text=golden[0].text,
            label=golden[0].label,
        )
        with pytest.raises(AssertionError, match="identical text"):
            generate.assert_disjoint(train + [disguised], golden)


class TestDatasetOnDisk:
    def test_round_trips_through_jsonl(self, tmp_path: Path) -> None:
        docs = generate.generate_documents(docs_per_class=3, seed=8)
        path = tmp_path / "docs.jsonl"
        written = generate.write_jsonl(docs, path)
        assert written == len(docs)
        assert generate.read_jsonl(path) == docs

    def test_generate_dataset_writes_everything(self, tmp_path: Path) -> None:
        manifest = generate.generate_dataset(
            tmp_path, docs_per_class=8, golden_per_class=2, seed=4
        )
        assert (tmp_path / "train.jsonl").is_file()
        assert (tmp_path / "golden.jsonl").is_file()
        assert (tmp_path / "shifted.jsonl").is_file()
        assert (tmp_path / "snapshot.json").is_file()

        stored = json.loads((tmp_path / "snapshot.json").read_text())
        assert stored["snapshot_id"] == manifest.snapshot_id
        assert stored["counts"]["train"] == 8 * len(DOCUMENT_CLASSES) - 2 * len(
            DOCUMENT_CLASSES
        )
        assert stored["counts"]["golden"] == 2 * len(DOCUMENT_CLASSES)

    def test_written_splits_are_disjoint(self, tmp_path: Path) -> None:
        generate.generate_dataset(
            tmp_path, docs_per_class=8, golden_per_class=2, seed=4
        )
        train = generate.read_jsonl(tmp_path / "train.jsonl")
        golden = generate.read_jsonl(tmp_path / "golden.jsonl")
        generate.assert_disjoint(train, golden)

    def test_no_shifted_flag_is_respected(self, tmp_path: Path) -> None:
        generate.generate_dataset(
            tmp_path,
            docs_per_class=8,
            golden_per_class=2,
            seed=4,
            include_shifted=False,
        )
        assert not (tmp_path / "shifted.jsonl").exists()
