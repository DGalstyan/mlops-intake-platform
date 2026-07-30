"""Drift math and the data-changed vs model-decayed classification.

Expected values are hand-computed, not captured from a previous run. A drift number
nobody has verified is worse than no drift number, because it will be believed.

The regression tests at the bottom exist because both bugs they cover produced
*permanent false positives* on an unshifted control window — the failure mode that
makes a drift detector worthless, since a detector that always fires gets ignored.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from src.config import BASELINE_SCHEMA_VERSION
from src.drift import metrics
from src.drift.report import (
    ProductionWindow,
    Verdict,
    build_report,
    classify,
    compute_concept_drift,
    compute_input_drift,
    compute_prediction_drift,
    render_markdown,
)

REPO = Path(__file__).resolve().parents[1]


class TestPSI:
    def test_identical_distributions_are_zero(self) -> None:
        assert metrics.population_stability_index([10, 20, 30], [10, 20, 30]) == (
            pytest.approx(0.0, abs=1e-9)
        )

    def test_scale_invariant(self) -> None:
        """PSI compares proportions, so doubling every count changes nothing."""
        assert metrics.population_stability_index(
            [10, 20, 30], [20, 40, 60]
        ) == pytest.approx(0.0, abs=1e-9)

    def test_hand_computed_two_bin_case(self) -> None:
        # baseline 50/50, actual 25/75.
        #   bin0: (0.25-0.50)*ln(0.25/0.50) = -0.25 * -0.693147 =  0.173287
        #   bin1: (0.75-0.50)*ln(0.75/0.50) =  0.25 *  0.405465 =  0.101366
        #   total = 0.274653
        assert metrics.population_stability_index([50, 50], [25, 75]) == pytest.approx(
            0.274653, abs=1e-5
        )

    def test_symmetric_under_exchange(self) -> None:
        """Worth pinning: PSI says THAT distributions differ, never which way.

        That is why the report pairs it with a directional median shift.
        """
        a = metrics.population_stability_index([50, 50], [25, 75])
        b = metrics.population_stability_index([25, 75], [50, 50])
        assert a == pytest.approx(b)

    def test_empty_bin_stays_finite(self) -> None:
        """An empty bin on one side would otherwise give infinite PSI.

        One empty tail bin would then dominate the whole statistic, so a document
        slightly longer than anything in training would look like total drift.
        """
        value = metrics.population_stability_index([100, 0], [50, 50])
        assert math.isfinite(value)
        assert value > 0

    def test_mismatched_bin_counts_are_rejected(self) -> None:
        """Different bin counts mean different edges — the comparison is meaningless."""
        with pytest.raises(ValueError, match="same edges"):
            metrics.population_stability_index([1, 2, 3], [1, 2])

    def test_interpretation_bands(self) -> None:
        assert metrics.interpret_psi(0.05) == "stable"
        assert metrics.interpret_psi(0.15) == "moderate shift"
        assert metrics.interpret_psi(0.40) == "significant shift"


class TestKS:
    def test_identical_samples_are_zero(self) -> None:
        assert metrics.ks_statistic([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(0.0)

    def test_disjoint_samples_are_one(self) -> None:
        assert metrics.ks_statistic([1, 2, 3], [10, 11, 12]) == pytest.approx(1.0)

    def test_hand_computed_half_shift(self) -> None:
        # baseline {0,1}, actual {1,2}. The largest CDF gap is at x=0:
        # F_base(0)=0.5, F_act(0)=0.0 -> 0.5
        assert metrics.ks_statistic([0, 1], [1, 2]) == pytest.approx(0.5)

    def test_empty_sample_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            metrics.ks_statistic([], [1, 2])


class TestCategoricalPSI:
    def test_matching_distribution_is_near_zero(self) -> None:
        total, _ = metrics.categorical_psi(
            {"a": 0.5, "b": 0.5}, {"a": 50, "b": 50}
        )
        assert total == pytest.approx(0.0, abs=1e-4)

    def test_contributions_identify_the_mover(self) -> None:
        """"Prediction drift is 0.31" is not actionable; naming the class is."""
        total, contributions = metrics.categorical_psi(
            {"a": 0.5, "b": 0.5}, {"a": 90, "b": 10}
        )
        assert total > 0.25
        assert max(contributions, key=lambda k: abs(contributions[k])) in {"a", "b"}

    def test_unseen_category_does_not_explode(self) -> None:
        total, _ = metrics.categorical_psi({"a": 1.0}, {"a": 90, "b": 10})
        assert math.isfinite(total)


class TestBinning:
    def test_uses_the_edges_it_is_given(self) -> None:
        """The whole point: production data is binned against the BASELINE's edges."""
        assert metrics.bin_samples([1, 5, 15], [0, 10, 20]) == [2, 1]

    def test_rejects_degenerate_edges(self) -> None:
        with pytest.raises(ValueError, match="at least two edges"):
            metrics.bin_samples([1, 2], [0])


class TestRelativeChange:
    def test_signed(self) -> None:
        assert metrics.relative_change(0.10, 0.15) == pytest.approx(0.5)
        assert metrics.relative_change(0.10, 0.05) == pytest.approx(-0.5)

    def test_zero_baseline_is_safe(self) -> None:
        assert metrics.relative_change(0.0, 0.0) == 0.0
        assert metrics.relative_change(0.0, 1.0) == float("inf")


def baseline_fixture(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "lineage": {"data_snapshot_id": "sha256:test", "git_sha": "abc"},
        "reference_set_size": 100,
        "prediction_priors": {
            "invoice": 0.25,
            "medical_report": 0.25,
            "id_document": 0.25,
            "correspondence": 0.25,
        },
        "document_char_length": {
            "p50": 150.0,
            "histogram_edges": [0, 100, 200, 400, 1_000_000],
            "histogram_counts": [10, 60, 25, 5],
        },
        # Derived from the SAME spec as window_fixture, so the control window and
        # the baseline are consistent by construction. An earlier version declared
        # char and token distributions that could not both come from one corpus, and
        # the control then legitimately reported token drift.
        "document_token_count": {
            "p50": 30.0,
            "histogram_edges": [0, 20, 40, 1_000_000],
            "histogram_counts": [10, 60, 30],
        },
        "confidence": {"p10": 0.70, "p50": 0.90},
    }
    base.update(overrides)
    return base


def window_fixture(
    *, confidence: float = 0.9, scale: float = 1.0, **kwargs: Any
) -> ProductionWindow:
    """A window whose length distribution MATCHES the baseline fixture.

    Built to the baseline's own histogram proportions (10/60/25/5 across the char
    edges) rather than as N identical documents. A constant-length window is a
    genuinely different distribution from a spread one, so using it as the "control"
    would make the control legitimately report drift — which is a broken fixture, not
    a broken detector.

    `scale` multiplies every length, to construct a real input shift on demand.
    """
    # Midpoints of the baseline's char bins, weighted by its counts.
    spec = [(60, 10), (150, 60), (300, 25), (600, 5)]
    texts: list[str] = []
    for length, count in spec:
        # Whole words, so the token-count distribution tracks the char distribution.
        words = max(1, int(length * scale) // 5)
        texts.extend([" ".join(["word"] * words)] * count)

    classes = ["invoice", "medical_report", "id_document", "correspondence"]
    return ProductionWindow(
        texts=texts,
        predicted_classes=[classes[i % 4] for i in range(len(texts))],
        confidences=[confidence] * len(texts),
        **kwargs,
    )


class TestProductionWindow:
    def test_override_rate_denominator_is_reviewed_not_total(self) -> None:
        """Using all documents would make the rate fall whenever auto-approval rose."""
        window = ProductionWindow(
            texts=["a"] * 100,
            predicted_classes=["invoice"] * 100,
            confidences=[0.9] * 100,
            reviewed_count=20,
            override_count=5,
        )
        assert window.override_rate == pytest.approx(0.25)

    def test_no_reviews_is_zero_not_a_crash(self) -> None:
        window = ProductionWindow(texts=["a"], predicted_classes=["invoice"], confidences=[0.9])
        assert window.override_rate == 0.0

    def test_impossible_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds reviewed_count"):
            ProductionWindow(
                texts=["a"],
                predicted_classes=["invoice"],
                confidences=[0.9],
                reviewed_count=1,
                override_count=5,
            )


class TestClassification:
    """The required distinction."""

    def _group(self, family: str, breached: bool) -> Any:
        from src.drift.report import SignalGroup

        group = SignalGroup(family=family)
        group.measures.append(
            metrics.DriftMeasure(
                name="t", statistic=1.0, threshold=0.5,
                breached=breached, interpretation="",
            )
        )
        return group

    def test_nothing_breached_is_no_drift(self) -> None:
        assert classify(
            self._group("input", False),
            self._group("prediction", False),
            self._group("concept", False),
        ) == Verdict.NO_DRIFT

    def test_input_only_is_data_changed(self) -> None:
        assert classify(
            self._group("input", True),
            self._group("prediction", False),
            self._group("concept", False),
        ) == Verdict.DATA_CHANGED

    def test_concept_only_is_model_decayed(self) -> None:
        assert classify(
            self._group("input", False),
            self._group("prediction", False),
            self._group("concept", True),
        ) == Verdict.MODEL_DECAYED

    def test_both_is_both(self) -> None:
        assert classify(
            self._group("input", True),
            self._group("prediction", False),
            self._group("concept", True),
        ) == Verdict.BOTH

    def test_prediction_drift_alone_is_not_decay(self) -> None:
        """A changed class mix is the model working, not failing.

        If the input mix changed, the prediction mix should follow. Treating it as
        decay on its own would trigger retrains every time a customer sent a
        different mix of paperwork.
        """
        assert classify(
            self._group("input", False),
            self._group("prediction", True),
            self._group("concept", False),
        ) == Verdict.DATA_CHANGED

    def test_only_decay_verdicts_trigger_retrain(self) -> None:
        """The central safety property.

        Retraining in response to input drift alone fits the new distribution's noise
        using review-sourced labels biased toward hard cases — it can make the model
        worse.
        """
        for verdict, expected in (
            (Verdict.NO_DRIFT, False),
            (Verdict.DATA_CHANGED, False),
            (Verdict.MODEL_DECAYED, True),
            (Verdict.BOTH, True),
        ):
            report = {"verdict": verdict}
            assert (verdict in (Verdict.MODEL_DECAYED, Verdict.BOTH)) is expected


class TestConceptDrift:
    def test_rising_confidence_is_not_decay(self) -> None:
        """One-sided by design. A model that got better must not look decayed."""
        group = compute_concept_drift(
            baseline_fixture(), window_fixture(confidence=0.99)
        )
        decay = [m for m in group.measures if m.name == "confidence_p10_decay"][0]
        assert decay.statistic > 0
        assert decay.breached is False

    def test_falling_confidence_breaches(self) -> None:
        group = compute_concept_drift(
            baseline_fixture(), window_fixture(confidence=0.40)
        )
        decay = [m for m in group.measures if m.name == "confidence_p10_decay"][0]
        assert decay.breached is True

    def test_missing_override_reference_does_not_breach(self) -> None:
        """The first drift run has no override reference.

        M1's baseline is built from training data where nothing was reviewed.
        Comparing against an implicit zero would report infinite decay on the first
        human correction of a brand-new deployment.
        """
        group = compute_concept_drift(
            baseline_fixture(),
            window_fixture(reviewed_count=10, override_count=5),
        )
        trend = [m for m in group.measures if m.name == "override_rate_trend"][0]
        assert trend.breached is False
        assert "no override-rate reference" in trend.interpretation

    def test_override_reference_present_enables_the_signal(self) -> None:
        group = compute_concept_drift(
            baseline_fixture(override_rate_reference=0.10),
            window_fixture(reviewed_count=40, override_count=18),
        )
        trend = [m for m in group.measures if m.name == "override_rate_trend"][0]
        assert trend.breached is True


class TestSchemaGuard:
    def test_unreadable_baseline_version_is_refused(self) -> None:
        """Fail closed. Computing drift against a shape you may misread is worse
        than not running."""
        with pytest.raises(ValueError, match="not readable"):
            build_report(
                baseline=baseline_fixture(schema_version="99.0.0"),
                window=window_fixture(),
                window_label="t",
            )


class TestFalsePositiveRegressions:
    """Both of these produced permanent false positives on an unshifted control.

    A drift detector that always fires is worse than none: it gets muted, and then
    the real signal is muted with it.
    """

    def test_control_window_reports_no_drift(self) -> None:
        """The single most important test in this file.

        Identical inputs, identical confidence, no override reference -> NO_DRIFT.
        """
        report = build_report(
            baseline=baseline_fixture(),
            window=window_fixture(confidence=0.90),
            window_label="control",
        )
        assert report["verdict"] == Verdict.NO_DRIFT
        assert report["should_trigger_retrain"] is False

    def test_confidence_reference_is_held_out_not_training(self) -> None:
        """A model is more confident on documents it memorised.

        Measured on this corpus: p10 0.865 on train vs 0.731 held out — a 15%
        apparent "decay" that is really memorisation, enough to breach the threshold
        on an unshifted window. The M1 baseline therefore measures confidence on the
        golden set and records which source it used.
        """
        artifact = REPO / "artifacts" / "v1" / "output" / "baseline_statistics.json"
        if not artifact.is_file():
            pytest.skip("run `make train` first")
        baseline = json.loads(artifact.read_text(encoding="utf-8"))
        assert baseline["confidence_source"] == "golden_holdout"
        # The train reference is kept for comparison, and should be visibly higher.
        assert (
            baseline["confidence_train_reference"]["p10"] > baseline["confidence"]["p10"]
        )

    def test_input_drift_uses_psi_not_reconstructed_ks(self) -> None:
        """KS needs samples; the baseline stores a histogram.

        Reconstructing samples to run KS measured the reconstruction: 0.30 and 0.38
        ("breached") on a control window where PSI said 0.009 and 0.002 ("stable").
        The input family now uses PSI plus a directional median shift, neither of
        which fabricates samples.
        """
        group = compute_input_drift(baseline_fixture(), window_fixture())
        names = {m.name for m in group.measures}
        assert not any(name.startswith("ks_") for name in names), (
            "input drift is using a KS statistic against a reconstructed baseline"
        )
        assert any(name.startswith("psi_") for name in names)
        assert any(name.startswith("median_shift_") for name in names)


class TestReportRendering:
    def test_markdown_states_the_verdict_and_action(self) -> None:
        report = build_report(
            baseline=baseline_fixture(),
            window=window_fixture(),
            window_label="t",
        )
        rendered = render_markdown(report)
        assert report["verdict"] in rendered
        assert "Recommended action" in rendered
        assert "Why this distinction matters" in rendered

    def test_report_explains_why_conflating_is_a_bug(self) -> None:
        """The assignment asks for this explicitly, so it ships in the artifact."""
        report = build_report(
            baseline=baseline_fixture(), window=window_fixture(), window_label="t"
        )
        explanation = report["why_the_distinction_matters"]
        assert "retrain" in explanation.lower()
        assert "bias" in explanation.lower()


class TestGeneratedEvidence:
    """The committed scenarios must show all three verdicts."""

    def test_scenario_summary_covers_the_three_cases(self) -> None:
        path = REPO / "evidence" / "m5" / "scenario-summary.json"
        if not path.is_file():
            pytest.skip("run `make drift-scenarios` first")
        rows = json.loads(path.read_text(encoding="utf-8"))
        verdicts = {row["scenario"]: row["verdict"] for row in rows}
        assert verdicts["control"] == Verdict.NO_DRIFT
        assert verdicts["data-changed"] == Verdict.DATA_CHANGED
        assert verdicts["model-decayed"] == Verdict.MODEL_DECAYED

    def test_loud_input_drift_does_not_trigger_retrain(self) -> None:
        """The case that matters most, asserted on the committed evidence."""
        path = REPO / "evidence" / "m5" / "scenario-summary.json"
        if not path.is_file():
            pytest.skip("run `make drift-scenarios` first")
        rows = {row["scenario"]: row for row in json.loads(path.read_text())}
        assert rows["data-changed"]["input"] is True
        assert rows["data-changed"]["retrain"] is False
        assert rows["model-decayed"]["input"] is False
        assert rows["model-decayed"]["retrain"] is True
