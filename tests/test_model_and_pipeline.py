"""The model interface, the baseline artifact, and the train/evaluate pipeline.

The tests that earn their place here are the ones that would catch a real
regression rather than restate the implementation:

- the interface is actually satisfied (so a swap cannot silently break callers),
- `predict_proba` column order matches `classes` (a mismatch mislabels every
  confidence in production while every metric still looks plausible),
- the baseline artifact refuses an unreadable schema version,
- the retrain gate blocks on a collapsed class even when macro-F1 improved.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.config import BASELINE_SCHEMA_VERSION, DOCUMENT_CLASSES
from src.data import generate
from src.training import baseline as baseline_module
from src.training import evaluate as evaluate_module
from src.training import lineage as lineage_module
from src.training import train as train_module
from src.training.model import (
    DocumentClassifier,
    TfidfLinearClassifier,
    build_classifier,
    load_classifier,
)


@pytest.fixture(scope="module")
def corpus() -> list[generate.Document]:
    # Enough per class that cv=3 isotonic calibration has something to work with.
    return generate.generate_documents(docs_per_class=40, seed=1234)


@pytest.fixture(scope="module")
def fitted(corpus: list[generate.Document]) -> TfidfLinearClassifier:
    model = TfidfLinearClassifier(seed=1234, min_df=1)
    model.fit([d.text for d in corpus], [d.label for d in corpus])
    return model


class TestInterfaceConformance:
    def test_implementation_satisfies_the_protocol(
        self, fitted: TfidfLinearClassifier
    ) -> None:
        assert isinstance(fitted, DocumentClassifier)

    def test_factory_returns_the_interface(self) -> None:
        assert isinstance(build_classifier(), DocumentClassifier)

    def test_unfitted_model_refuses_to_predict(self) -> None:
        with pytest.raises(RuntimeError, match="not fitted"):
            TfidfLinearClassifier().predict(["anything"])

    def test_unfitted_model_has_no_classes(self) -> None:
        with pytest.raises(RuntimeError, match="not fitted"):
            _ = TfidfLinearClassifier().classes


class TestFitValidation:
    def test_rejects_mismatched_lengths(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            TfidfLinearClassifier().fit(["a", "b"], ["invoice"])

    def test_rejects_empty_dataset(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            TfidfLinearClassifier().fit([], [])

    def test_rejects_unknown_class_label(self) -> None:
        """A label outside DOCUMENT_CLASSES must fail at fit time.

        Otherwise the model silently learns a class the confusion matrix, the
        baseline priors and the per-class gate floor all have no column for.
        """
        with pytest.raises(ValueError, match="not in config.DOCUMENT_CLASSES"):
            TfidfLinearClassifier().fit(["some text"], ["not_a_real_class"])


class TestProbabilities:
    def test_rows_sum_to_one(self, fitted: TfidfLinearClassifier) -> None:
        proba = fitted.predict_proba(["invoice amount due payable vat"])
        assert proba.sum(axis=1) == pytest.approx(1.0)

    def test_shape_matches_classes(self, fitted: TfidfLinearClassifier) -> None:
        proba = fitted.predict_proba(["a", "b"])
        assert proba.shape == (2, len(fitted.classes))

    def test_argmax_agrees_with_predict(self, fitted: TfidfLinearClassifier) -> None:
        """Column order of predict_proba must match `classes`.

        If these disagree, every confidence in production is attributed to the
        wrong class — the Route state gates on a number belonging to a different
        label — while accuracy and macro-F1 stay unchanged, so nothing else
        reveals it.
        """
        texts = [d.text for d in generate.generate_documents(docs_per_class=3, seed=99)]
        proba = fitted.predict_proba(texts)
        predicted = fitted.predict(texts)
        classes = fitted.classes
        from_proba = [classes[i] for i in np.argmax(proba, axis=1)]
        assert from_proba == predicted

    def test_probabilities_are_in_range(self, fitted: TfidfLinearClassifier) -> None:
        proba = fitted.predict_proba(["invoice total due"])
        assert np.all(proba >= 0.0) and np.all(proba <= 1.0)


class TestPersistence:
    def test_round_trip_preserves_predictions(
        self, fitted: TfidfLinearClassifier, tmp_path: Path
    ) -> None:
        path = tmp_path / "model.joblib"
        fitted.save(path)
        reloaded = load_classifier(path)

        texts = [d.text for d in generate.generate_documents(docs_per_class=2, seed=7)]
        assert reloaded.predict(texts) == fitted.predict(texts)
        assert np.allclose(reloaded.predict_proba(texts), fitted.predict_proba(texts))

    def test_load_dispatches_on_recorded_implementation(
        self, fitted: TfidfLinearClassifier, tmp_path: Path
    ) -> None:
        path = tmp_path / "model.joblib"
        fitted.save(path)
        assert isinstance(load_classifier(path), TfidfLinearClassifier)

    def test_load_rejects_an_unknown_implementation(self, tmp_path: Path) -> None:
        import joblib

        path = tmp_path / "alien.joblib"
        joblib.dump({"implementation": "SomeFutureTransformer", "pipeline": None}, path)
        with pytest.raises(ValueError, match="no loader registered"):
            load_classifier(path)


class TestBaselineArtifact:
    def _build(self) -> dict[str, object]:
        return baseline_module.build_baseline(
            texts=["short doc", "a somewhat longer document here"],
            predictions=["invoice", "correspondence"],
            confidences=[0.9, 0.6],
            labels=list(DOCUMENT_CLASSES),
            snapshot_id="sha256:abc",
            git_sha="deadbeef",
        )

    def test_records_distributions_not_just_accuracy(self) -> None:
        """The named failure mode: a baseline that stores only a score.

        Drift is a change in a distribution, so a baseline holding a scalar
        cannot support any drift test at all.
        """
        artifact = self._build()
        for key in (
            "prediction_priors",
            "document_char_length",
            "document_token_count",
            "confidence",
        ):
            assert key in artifact
        assert "accuracy" not in artifact
        assert "macro_f1" not in artifact

    def test_priors_sum_to_one(self) -> None:
        priors = self._build()["prediction_priors"]
        assert isinstance(priors, dict)
        assert sum(priors.values()) == pytest.approx(1.0)

    def test_histogram_edges_are_stored_with_the_artifact(self) -> None:
        """Edges must travel with the baseline.

        Recomputing bin edges from live production data would compare two
        differently-binned distributions and manufacture drift out of nothing.
        """
        length = self._build()["document_char_length"]
        assert isinstance(length, dict)
        assert length["histogram_edges"]
        assert len(length["histogram_counts"]) == len(length["histogram_edges"]) - 1

    def test_carries_lineage(self) -> None:
        lineage = self._build()["lineage"]
        assert isinstance(lineage, dict)
        assert lineage["data_snapshot_id"] == "sha256:abc"
        assert lineage["git_sha"] == "deadbeef"

    def test_rejects_mismatched_input_lengths(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            baseline_module.build_baseline(
                texts=["one"],
                predictions=["invoice", "invoice"],
                confidences=[0.5],
                labels=list(DOCUMENT_CLASSES),
                snapshot_id="s",
                git_sha="g",
            )

    def test_rejects_unknown_predicted_class(self) -> None:
        with pytest.raises(ValueError, match="not a known class"):
            baseline_module.build_baseline(
                texts=["one"],
                predictions=["martian_document"],
                confidences=[0.5],
                labels=list(DOCUMENT_CLASSES),
                snapshot_id="s",
                git_sha="g",
            )

    def test_loader_accepts_the_current_version(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(self._build()), encoding="utf-8")
        assert baseline_module.load_baseline(path)["schema_version"] == (
            BASELINE_SCHEMA_VERSION
        )

    def test_loader_refuses_a_future_major_version(self, tmp_path: Path) -> None:
        """Fail closed on an unknown schema.

        A drift job that reads a shape it does not understand reports numbers
        computed against the wrong fields, which is worse than not running.
        """
        artifact = self._build()
        artifact["schema_version"] = "99.0.0"
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        with pytest.raises(ValueError, match="cannot read"):
            baseline_module.load_baseline(path)

    def test_loader_refuses_a_missing_version(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"prediction_priors": {}}), encoding="utf-8")
        with pytest.raises(ValueError, match="no schema_version"):
            baseline_module.load_baseline(path)


class TestRetrainGate:
    """The gate is M5's, but its logic and thresholds live in M1's evaluate."""

    def _metrics(self, macro_f1: float, per_class: dict[str, float]) -> dict[str, object]:
        return {
            "macro_f1": macro_f1,
            "per_class": [
                {"label": label, "f1": f1} for label, f1 in per_class.items()
            ],
        }

    def test_blocks_an_improvement_below_the_margin(self) -> None:
        """A tiny gain must not pass.

        On a 240-document golden set the difference between two runs is often
        noise, and a gate that fires on noise trains people to ignore it.
        """
        candidate = self._metrics(0.905, {label: 0.9 for label in DOCUMENT_CLASSES})
        champion = self._metrics(0.900, {label: 0.9 for label in DOCUMENT_CLASSES})
        result = evaluate_module.evaluate_gate(candidate, champion)
        assert result["passed"] is False
        assert "below the required" in str(result["reason"])

    def test_passes_a_clear_improvement(self) -> None:
        candidate = self._metrics(0.95, {label: 0.94 for label in DOCUMENT_CLASSES})
        champion = self._metrics(0.90, {label: 0.9 for label in DOCUMENT_CLASSES})
        assert evaluate_module.evaluate_gate(candidate, champion)["passed"] is True

    def test_blocks_a_collapsed_class_despite_a_macro_f1_gain(self) -> None:
        """The case macro-averaging alone does not catch.

        Overall macro-F1 improves by well over the margin, but one class has
        fallen through the floor. A model that got better on average while
        becoming useless for id_document must not ship.
        """
        per_class = {label: 0.99 for label in DOCUMENT_CLASSES}
        per_class["id_document"] = 0.10
        candidate = self._metrics(0.97, per_class)
        champion = self._metrics(0.90, {label: 0.9 for label in DOCUMENT_CLASSES})

        result = evaluate_module.evaluate_gate(candidate, champion)
        assert result["passed"] is False
        assert "per-class floor" in str(result["reason"])
        failing = result["failing_classes"]
        assert isinstance(failing, list)
        assert failing[0]["label"] == "id_document"

    def test_first_version_applies_only_the_floor(self) -> None:
        candidate = self._metrics(0.5, {label: 0.7 for label in DOCUMENT_CLASSES})
        result = evaluate_module.evaluate_gate(candidate, None)
        assert result["is_first_version"] is True
        assert result["passed"] is True
        assert result["champion_macro_f1"] is None

    def test_first_version_still_blocked_by_the_floor(self) -> None:
        candidate = self._metrics(0.5, {label: 0.1 for label in DOCUMENT_CLASSES})
        result = evaluate_module.evaluate_gate(candidate, None)
        assert result["passed"] is False

    def test_a_regression_is_blocked(self) -> None:
        candidate = self._metrics(0.80, {label: 0.8 for label in DOCUMENT_CLASSES})
        champion = self._metrics(0.90, {label: 0.9 for label in DOCUMENT_CLASSES})
        result = evaluate_module.evaluate_gate(candidate, champion)
        assert result["passed"] is False
        assert float(result["actual_improvement"]) < 0  # type: ignore[arg-type]


class TestLineage:
    def test_missing_values_are_named_not_defaulted(self) -> None:
        record = lineage_module.Lineage(
            data_snapshot_id=lineage_module.UNKNOWN,
            git_sha="abc123",
            training_image_digest=lineage_module.UNKNOWN,
            environment={},
            hyperparameters={},
        )
        assert record.is_complete() is False
        assert set(record.missing_fields()) == {
            "data_snapshot_id",
            "training_image_digest",
        }

    def test_complete_lineage_reports_complete(self) -> None:
        record = lineage_module.Lineage(
            data_snapshot_id="sha256:a",
            git_sha="abc",
            training_image_digest="repo@sha256:b",
            environment={},
            hyperparameters={},
        )
        assert record.is_complete() is True
        assert record.missing_fields() == []

    def test_git_sha_prefers_injected_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A container has no git history, so CI injects the SHA."""
        monkeypatch.setenv("GIT_SHA", "injected-sha")
        assert lineage_module.resolve_git_sha() == "injected-sha"

    def test_snapshot_id_is_unknown_when_no_manifest(self, tmp_path: Path) -> None:
        assert (
            lineage_module.read_snapshot_id(tmp_path, manifest_filename="snapshot.json")
            == lineage_module.UNKNOWN
        )

    def test_snapshot_id_read_from_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "snapshot.json").write_text(
            json.dumps({"snapshot_id": "sha256:xyz"}), encoding="utf-8"
        )
        assert (
            lineage_module.read_snapshot_id(tmp_path, manifest_filename="snapshot.json")
            == "sha256:xyz"
        )


class TestEndToEndTrainEvaluate:
    """Train then evaluate on generated data, through the real entrypoints."""

    def test_produces_all_artifacts_and_a_held_out_score(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        generate.generate_dataset(
            data_dir, docs_per_class=40, golden_per_class=10, seed=555
        )

        model_dir = tmp_path / "model"
        output_dir = tmp_path / "output"
        assert (
            train_module.main(
                [
                    "--train-dir", str(data_dir),
                    "--model-dir", str(model_dir),
                    "--output-dir", str(output_dir),
                    "--seed", "555",
                ]
            )
            == 0
        )

        assert (model_dir / "model.joblib").is_file()
        assert (output_dir / "metrics.json").is_file()
        assert (output_dir / "baseline_statistics.json").is_file()
        assert (output_dir / "lineage.json").is_file()

        # The training metrics must be labelled as not held out, so nothing
        # downstream can mistake them for a gate-worthy score.
        train_metrics = json.loads((output_dir / "metrics.json").read_text())
        assert train_metrics["split"] == "train"
        assert train_metrics["is_held_out"] is False

        eval_dir = tmp_path / "eval"
        assert (
            evaluate_module.main(
                [
                    "--model-dir", str(model_dir),
                    "--data-dir", str(data_dir),
                    "--output-dir", str(eval_dir),
                ]
            )
            == 0
        )

        golden_metrics = json.loads((eval_dir / "metrics.json").read_text())
        assert golden_metrics["split"] == "golden"
        assert golden_metrics["is_held_out"] is True
        assert golden_metrics["non_overlap_verified"] is True
        assert golden_metrics["n_samples"] == 10 * len(DOCUMENT_CLASSES)
        # Accuracy is a non-goal, but a model that cannot beat chance on
        # deliberately separable synthetic data indicates a wiring bug.
        assert golden_metrics["macro_f1"] > 0.5

    def test_evaluate_fails_loudly_on_leaked_golden_set(self, tmp_path: Path) -> None:
        """The most important negative test in the suite.

        Leakage inflates every downstream number in the direction that looks like
        success, so this assertion is the only thing that catches it.
        """
        data_dir = tmp_path / "data"
        generate.generate_dataset(
            data_dir, docs_per_class=30, golden_per_class=8, seed=99
        )

        model_dir = tmp_path / "model"
        train_module.main(
            [
                "--train-dir", str(data_dir),
                "--model-dir", str(model_dir),
                "--output-dir", str(tmp_path / "out"),
                "--seed", "99",
            ]
        )

        # Poison the training split with golden documents.
        train = generate.read_jsonl(data_dir / "train.jsonl")
        golden = generate.read_jsonl(data_dir / "golden.jsonl")
        generate.write_jsonl(train + golden, data_dir / "train.jsonl")

        with pytest.raises(AssertionError, match="both train and golden"):
            evaluate_module.main(
                [
                    "--model-dir", str(model_dir),
                    "--data-dir", str(data_dir),
                    "--output-dir", str(tmp_path / "eval"),
                ]
            )

    def test_two_runs_are_distinguishable(self, tmp_path: Path) -> None:
        """The M1 deliverable: two versions with different metrics.

        Disabling calibration is the axis chosen, because it changes ECE — the
        metric the confidence gate depends on — rather than shifting macro-F1 by
        random noise.
        """
        data_dir = tmp_path / "data"
        generate.generate_dataset(
            data_dir, docs_per_class=40, golden_per_class=10, seed=777
        )

        scores: list[float] = []
        for run, extra in enumerate([[], ["--no-calibration"]]):
            model_dir = tmp_path / f"model{run}"
            eval_dir = tmp_path / f"eval{run}"
            train_module.main(
                [
                    "--train-dir", str(data_dir),
                    "--model-dir", str(model_dir),
                    "--output-dir", str(tmp_path / f"out{run}"),
                    "--seed", "777",
                    *extra,
                ]
            )
            evaluate_module.main(
                [
                    "--model-dir", str(model_dir),
                    "--data-dir", str(data_dir),
                    "--output-dir", str(eval_dir),
                ]
            )
            metrics_doc = json.loads((eval_dir / "metrics.json").read_text())
            scores.append(float(metrics_doc["expected_calibration_error"]))

        assert scores[0] != scores[1], (
            "calibrated and uncalibrated runs produced identical ECE — the two "
            "registry versions would not be distinguishable"
        )

    def test_train_is_reproducible_for_a_fixed_seed(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        generate.generate_dataset(
            data_dir, docs_per_class=30, golden_per_class=8, seed=31337
        )

        results: list[str] = []
        for run in range(2):
            model_dir = tmp_path / f"m{run}"
            eval_dir = tmp_path / f"e{run}"
            train_module.main(
                [
                    "--train-dir", str(data_dir),
                    "--model-dir", str(model_dir),
                    "--output-dir", str(tmp_path / f"o{run}"),
                    "--seed", "31337",
                ]
            )
            evaluate_module.main(
                [
                    "--model-dir", str(model_dir),
                    "--data-dir", str(data_dir),
                    "--output-dir", str(eval_dir),
                ]
            )
            doc = json.loads((eval_dir / "metrics.json").read_text())
            results.append(f"{doc['macro_f1']:.10f}/{doc['expected_calibration_error']:.10f}")

        assert results[0] == results[1], "identical seed produced different metrics"
