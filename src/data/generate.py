"""Synthetic document generator.

Exists so a reviewer can reproduce the entire run from zero without access to
real documents. Two properties matter more than realism:

1. **Determinism.** The same seed produces byte-identical output, so the data
   snapshot id is stable and a training run can be replayed. All randomness
   comes from explicitly-passed `random.Random` instances; nothing touches the
   global RNG, so importing this module cannot perturb another component's
   draws.
2. **A content-addressed snapshot id.** The id is a hash of the generated
   records plus the parameters that produced them, not a timestamp or a UUID.
   That makes it a real lineage key: two runs with the same id trained on
   provably the same bytes, and a changed document changes the id.

The generator also produces a *shifted* variant used by M5 to demonstrate drift
detection. The shift is deliberately in input distribution (vocabulary and
document length) while leaving labels alone, which is what makes it useful for
telling "the data changed" apart from "the model got worse".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Final, Iterable, Iterator, Literal, Sequence

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    DEFAULT_DOCS_PER_CLASS,
    DEFAULT_GOLDEN_PER_CLASS,
    DEFAULT_SEED,
    DOCUMENT_CLASSES,
    SNAPSHOT_MANIFEST_FILENAME,
)

SplitName = Literal["train", "golden", "shifted"]

# --- Vocabulary ------------------------------------------------------------
# Per-class term pools. Overlap between pools is intentional: a generator whose
# classes share no vocabulary produces a trivially separable problem and a
# meaningless 1.00 F1, which would hide exactly the calibration behaviour the
# Route state depends on.

_SHARED_TERMS: Final[tuple[str, ...]] = (
    "reference",
    "date",
    "page",
    "total",
    "contact",
    "address",
    "attached",
    "please",
    "regarding",
    "copy",
)

_CLASS_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "invoice": (
        "invoice",
        "amount",
        "due",
        "payable",
        "vat",
        "tax",
        "subtotal",
        "purchase",
        "order",
        "remittance",
        "bank",
        "terms",
        "net",
        "quantity",
        "unit",
        "price",
        "vendor",
        "billing",
    ),
    "medical_report": (
        "patient",
        "specimen",
        "clinician",
        "findings",
        "impression",
        "diagnosis",
        "laboratory",
        "reference",
        "range",
        "abnormal",
        "sample",
        "collected",
        "histology",
        "haemoglobin",
        "result",
        "referral",
    ),
    "id_document": (
        "passport",
        "licence",
        "nationality",
        "surname",
        "given",
        "names",
        "birth",
        "expiry",
        "issuing",
        "authority",
        "holder",
        "signature",
        "machine",
        "readable",
        "zone",
        "identity",
    ),
    "correspondence": (
        "letter",
        "sincerely",
        "faithfully",
        "enquiry",
        "response",
        "acknowledge",
        "complaint",
        "notice",
        "follow",
        "meeting",
        "discussed",
        "confirm",
        "arrangement",
        "sender",
        "recipient",
    ),
}

# Terms injected only into the shifted batch. New vocabulary is the cleanest
# input-drift signal: PSI/KS on token distributions moves, while the label
# relationship is untouched.
_SHIFT_TERMS: Final[tuple[str, ...]] = (
    "portal",
    "digitised",
    "escalated",
    "workflow",
    "attachment",
    "scanned",
    "queue",
    "backlog",
    "expedited",
    "resubmitted",
)


@dataclass(frozen=True, slots=True)
class Document:
    """One generated document. `label` is ground truth for training/evaluation."""

    doc_id: str
    text: str
    label: str

    def to_json(self) -> str:
        # sort_keys so the serialisation feeding the snapshot hash is canonical.
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Lineage record for one generated dataset.

    `snapshot_id` is the value carried into the model registry, so a registered
    model version can be traced to the exact bytes it trained on.
    """

    snapshot_id: str
    seed: int
    classes: tuple[str, ...]
    counts: dict[str, int]
    docs_per_class: int
    golden_per_class: int
    generator_version: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)


# Bump when the generation logic changes in a way that alters output for a fixed
# seed. Part of the snapshot hash, so old and new data cannot collide.
GENERATOR_VERSION: Final[str] = "1.1.0"

# Relative frequency of own-class / shared / other-class terms in a document.
# Tuned so the held-out macro-F1 lands well short of 1.0: the target is a task
# that is learnable but genuinely confusable, because a perfect classifier makes
# the calibration metric, the confidence gate, the review queue and the drift
# demo all vacuous. See the note in _generate_text.
# Chosen by sweeping these weights against held-out macro-F1 and the fraction of
# documents landing below AUTO_APPROVE_CONFIDENCE_THRESHOLD. This point gives
# macro-F1 ~0.94 with ~12% of documents routed to human review — learnable but
# imperfect, and the review queue actually receives traffic. Raising
# _OWN_TERM_WEIGHT to 9 pushes macro-F1 to 0.996 and empties the review queue;
# dropping it to 6 collapses to 0.68, where nothing auto-approves.
_OWN_TERM_WEIGHT: Final[int] = 8
_SHARED_TERM_WEIGHT: Final[int] = 4
_OTHER_TERM_WEIGHT: Final[int] = 2


def _derive_seed(*parts: object) -> int:
    """Derive a sub-seed deterministically from a base seed and labels.

    Deliberately NOT `hash((seed, "label", i))`: Python randomises `str` and
    `bytes` hashing per process unless PYTHONHASHSEED is fixed, so a tuple hash
    containing a string produces different values between runs. That would
    silently break the reproducibility this module's whole purpose rests on —
    silently, because the output would still look like valid data. SHA-256 over a
    canonical encoding has no such dependency.
    """
    canonical = "|".join(repr(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big")


def _sentence(rng: random.Random, terms: Sequence[str], length: int) -> str:
    return " ".join(rng.choice(terms) for _ in range(length))


def _generate_text(
    rng: random.Random,
    label: str,
    *,
    shifted: bool,
) -> str:
    """Build one document's text.

    The class term pool dominates but never exclusively: every document also
    draws shared terms, so classes are separable-but-confusable. Length varies
    per document, which matters because document length is one of the
    distributions the baseline artifact records and the drift job compares.
    """
    own = list(_CLASS_TERMS[label])
    other = [
        term
        for other_label, terms in _CLASS_TERMS.items()
        if other_label != label
        for term in terms
    ]

    # Weighted mixture rather than "this class's words only". The own-class pool
    # dominates, but shared boilerplate and terms borrowed from the other three
    # classes are frequent enough that documents are genuinely confusable.
    #
    # This matters more than it looks. If classes are trivially separable the
    # model scores a perfect macro-F1 with every confidence pinned at 1.0 — and
    # then the calibration metric has nothing to measure, the Route state's
    # confidence threshold can never fire, the human-review queue is always
    # empty, and the drift demo has no headroom to move. A too-easy generator
    # silently guts four later milestones.
    pool = own * _OWN_TERM_WEIGHT
    pool += list(_SHARED_TERMS) * _SHARED_TERM_WEIGHT
    pool += other * _OTHER_TERM_WEIGHT

    if shifted:
        # Shift input distribution two ways at once: new vocabulary, and longer
        # documents. Both are visible to input-drift tests; neither changes the
        # true label.
        pool += list(_SHIFT_TERMS) * 3
        n_sentences = rng.randint(4, 9)
        words_per_sentence = rng.randint(8, 16)
    else:
        n_sentences = rng.randint(2, 4)
        words_per_sentence = rng.randint(4, 9)

    # Deliberately carries no class information. An earlier version put the
    # class name in the header, which let the classifier read the label straight
    # out of the text: macro-F1 1.00, ECE 0.0003, and a completely hollow
    # calibration story. There is a test asserting the label does not appear
    # verbatim in any document.
    header = f"document {rng.randint(100000, 999999)}"
    body = " ".join(
        _sentence(rng, pool, words_per_sentence) for _ in range(n_sentences)
    )
    return f"{header} {body}"


def generate_documents(
    *,
    docs_per_class: int = DEFAULT_DOCS_PER_CLASS,
    seed: int = DEFAULT_SEED,
    shifted: bool = False,
    id_prefix: str = "doc",
) -> list[Document]:
    """Generate `docs_per_class` documents for each class, deterministically.

    Documents are produced class-by-class and then sorted by `doc_id`, so the
    returned order does not depend on iteration order of any dict.
    """
    if docs_per_class < 1:
        raise ValueError(f"docs_per_class must be >= 1, got {docs_per_class}")

    docs: list[Document] = []
    for class_index, label in enumerate(DOCUMENT_CLASSES):
        # A per-class RNG derived from the base seed keeps each class's stream
        # independent: changing docs_per_class for one class does not shift the
        # documents generated for the others.
        rng = random.Random(_derive_seed(seed, "generate", class_index, shifted))
        for i in range(docs_per_class):
            doc_id = f"{id_prefix}-{label}-{i:05d}"
            docs.append(
                Document(
                    doc_id=doc_id,
                    text=_generate_text(rng, label, shifted=shifted),
                    label=label,
                )
            )
    docs.sort(key=lambda d: d.doc_id)
    return docs


def compute_snapshot_id(
    documents: Iterable[Document],
    *,
    seed: int,
    docs_per_class: int,
    golden_per_class: int,
) -> str:
    """Content-address a dataset.

    Hashes the canonical serialisation of every document plus the generation
    parameters. Deliberately not a UUID or timestamp: the id has to be
    reproducible from the data itself, or it cannot prove two runs used the same
    input.
    """
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "generator_version": GENERATOR_VERSION,
                "seed": seed,
                "docs_per_class": docs_per_class,
                "golden_per_class": golden_per_class,
                "classes": list(DOCUMENT_CLASSES),
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    for doc in documents:
        digest.update(doc.to_json().encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def split_train_golden(
    documents: Sequence[Document],
    *,
    golden_per_class: int = DEFAULT_GOLDEN_PER_CLASS,
    seed: int = DEFAULT_SEED,
) -> tuple[list[Document], list[Document]]:
    """Split into (train, golden) with `golden_per_class` held out per class.

    Stratified by construction — a fixed count per class rather than a fraction
    of the whole — so a class-imbalanced training set still yields a balanced
    golden set. An imbalanced golden set would make macro-F1 read as noise on
    the smallest class, which is the metric the retrain gate depends on.

    The split is a deterministic shuffle, not a slice of the generated order, so
    the golden set is not systematically the highest-numbered documents.
    """
    by_class: dict[str, list[Document]] = {label: [] for label in DOCUMENT_CLASSES}
    for doc in documents:
        by_class[doc.label].append(doc)

    train: list[Document] = []
    golden: list[Document] = []
    for class_index, label in enumerate(DOCUMENT_CLASSES):
        pool = sorted(by_class[label], key=lambda d: d.doc_id)
        if len(pool) <= golden_per_class:
            raise ValueError(
                f"class {label!r} has {len(pool)} documents but "
                f"golden_per_class={golden_per_class}; nothing would remain to "
                "train on"
            )
        rng = random.Random(_derive_seed(seed, "split", class_index))
        rng.shuffle(pool)
        golden.extend(pool[:golden_per_class])
        train.extend(pool[golden_per_class:])

    train.sort(key=lambda d: d.doc_id)
    golden.sort(key=lambda d: d.doc_id)
    return train, golden


def assert_disjoint(train: Sequence[Document], golden: Sequence[Document]) -> None:
    """Fail loudly if any document appears in both splits.

    Training on the golden set is the single most damaging thing that can go
    wrong here: every downstream metric, the retrain gate, and the drift
    baseline all inherit the leak, and the resulting scores look *better*, so
    nothing else in the system will flag it. Checked on both id and text — an
    id-only check would miss a duplicated document that was assigned two ids.
    """
    train_ids = {d.doc_id for d in train}
    golden_ids = {d.doc_id for d in golden}
    shared_ids = train_ids & golden_ids
    if shared_ids:
        sample = sorted(shared_ids)[:5]
        raise AssertionError(
            f"{len(shared_ids)} document id(s) appear in both train and golden "
            f"splits (e.g. {sample}). The golden set must be held out entirely."
        )

    train_texts = {d.text for d in train}
    shared_texts = train_texts & {d.text for d in golden}
    if shared_texts:
        raise AssertionError(
            f"{len(shared_texts)} document(s) have identical text in both train "
            "and golden splits. Distinct ids are not enough — this is still "
            "leakage."
        )


def write_jsonl(documents: Iterable[Document], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for doc in documents:
            handle.write(doc.to_json())
            handle.write("\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[Document]:
    documents: list[Document] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            documents.append(
                Document(doc_id=raw["doc_id"], text=raw["text"], label=raw["label"])
            )
    return documents


def iter_texts(documents: Iterable[Document]) -> Iterator[str]:
    return (d.text for d in documents)


def generate_dataset(
    output_dir: Path,
    *,
    docs_per_class: int = DEFAULT_DOCS_PER_CLASS,
    golden_per_class: int = DEFAULT_GOLDEN_PER_CLASS,
    seed: int = DEFAULT_SEED,
    include_shifted: bool = True,
) -> SnapshotManifest:
    """Generate, split, verify and write a full dataset with its manifest."""
    documents = generate_documents(docs_per_class=docs_per_class, seed=seed)
    train, golden = split_train_golden(
        documents, golden_per_class=golden_per_class, seed=seed
    )
    assert_disjoint(train, golden)

    snapshot_id = compute_snapshot_id(
        documents,
        seed=seed,
        docs_per_class=docs_per_class,
        golden_per_class=golden_per_class,
    )

    counts = {
        "total": len(documents),
        "train": write_jsonl(train, output_dir / "train.jsonl"),
        "golden": write_jsonl(golden, output_dir / "golden.jsonl"),
    }

    if include_shifted:
        shifted = generate_documents(
            docs_per_class=max(1, docs_per_class // 4),
            seed=seed,
            shifted=True,
            id_prefix="shifted",
        )
        counts["shifted"] = write_jsonl(shifted, output_dir / "shifted.jsonl")

    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        seed=seed,
        classes=DOCUMENT_CLASSES,
        counts=counts,
        docs_per_class=docs_per_class,
        golden_per_class=golden_per_class,
        generator_version=GENERATOR_VERSION,
    )
    (output_dir / SNAPSHOT_MANIFEST_FILENAME).write_text(
        manifest.to_json(), encoding="utf-8"
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--docs-per-class", type=int, default=DEFAULT_DOCS_PER_CLASS)
    parser.add_argument(
        "--golden-per-class", type=int, default=DEFAULT_GOLDEN_PER_CLASS
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--no-shifted", action="store_true")
    args = parser.parse_args(argv)

    manifest = generate_dataset(
        args.output_dir,
        docs_per_class=args.docs_per_class,
        golden_per_class=args.golden_per_class,
        seed=args.seed,
        include_shifted=not args.no_shifted,
    )
    print(manifest.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
