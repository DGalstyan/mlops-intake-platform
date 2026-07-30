"""Metric and calibration math against hand-computed values.

Every expected number here is worked out by hand in the test, not captured from a
previous run of the code under test. A golden-file test that records whatever the
implementation produced would pass just as happily with the formula wrong.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from numpy.typing import NDArray

from src.training import metrics

LABELS = ("a", "b", "c")


class TestConfusionMatrix:
    def test_counts_land_in_the_right_cells(self) -> None:
        # a->a, a->b, b->b, c->a
        y_true = ["a", "a", "b", "c"]
        y_pred = ["a", "b", "b", "a"]
        matrix = metrics.confusion_matrix(y_true, y_pred, LABELS)

        expected = np.array(
            [
                [1, 1, 0],  # true a: one correct, one predicted b
                [0, 1, 0],  # true b: one correct
                [1, 0, 0],  # true c: one predicted a
            ],
            dtype=np.int64,
        )
        assert np.array_equal(matrix, expected)

    def test_rows_are_true_classes_not_predictions(self) -> None:
        # One document, truly 'a', predicted 'b'. Row a / column b must be 1,
        # and row b / column a must be 0 — this catches a transposed matrix,
        # which is invisible in a symmetric test case.
        matrix = metrics.confusion_matrix(["a"], ["b"], LABELS)
        assert matrix[0, 1] == 1
        assert matrix[1, 0] == 0

    def test_unknown_label_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown label"):
            metrics.confusion_matrix(["z"], ["a"], LABELS)

    def test_length_mismatch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            metrics.confusion_matrix(["a", "b"], ["a"], LABELS)


class TestPerClassMetrics:
    def test_precision_recall_f1_by_hand(self) -> None:
        # Class 'a': predicted 3 times, 2 correct; truly present 2 times, both found.
        #   precision = 2/3, recall = 2/2 = 1.0
        #   f1 = 2 * (2/3 * 1) / (2/3 + 1) = (4/3) / (5/3) = 0.8
        y_true = ["a", "a", "b", "c"]
        y_pred = ["a", "a", "a", "c"]
        results = {m.label: m for m in metrics.per_class_metrics(y_true, y_pred, LABELS)}

        assert results["a"].precision == pytest.approx(2 / 3)
        assert results["a"].recall == pytest.approx(1.0)
        assert results["a"].f1 == pytest.approx(0.8)
        assert results["a"].support == 2

    def test_class_never_predicted_scores_zero_not_nan(self) -> None:
        # 'b' is truly present once but never predicted: recall 0, precision 0
        # (no predictions), f1 0. NaN here would poison macro_f1 and make the
        # retrain gate undefined exactly when a class has collapsed.
        results = {
            m.label: m for m in metrics.per_class_metrics(["b"], ["a"], LABELS)
        }
        assert results["b"].precision == 0.0
        assert results["b"].recall == 0.0
        assert results["b"].f1 == 0.0
        assert not math.isnan(results["b"].f1)

    def test_absent_class_has_zero_support(self) -> None:
        results = {
            m.label: m for m in metrics.per_class_metrics(["a"], ["a"], LABELS)
        }
        assert results["c"].support == 0
        assert results["c"].f1 == 0.0


class TestMacroF1:
    def test_is_unweighted_mean_of_per_class_f1(self) -> None:
        y_true = ["a", "a", "b", "c"]
        y_pred = ["a", "a", "a", "c"]
        per_class = metrics.per_class_metrics(y_true, y_pred, LABELS)
        expected = sum(m.f1 for m in per_class) / len(per_class)

        assert metrics.macro_f1(y_true, y_pred, LABELS) == pytest.approx(expected)

    def test_perfect_prediction_is_one(self) -> None:
        y = ["a", "b", "c"]
        assert metrics.macro_f1(y, y, LABELS) == pytest.approx(1.0)

    def test_a_collapsed_minority_class_drags_macro_f1_down(self) -> None:
        # This is the property the per-class gate floor exists to exploit: 9 of
        # 10 documents are class 'a' and all are correct, but 'b' is never found.
        # Accuracy is 0.9; macro-F1 must be far lower, or the metric would let a
        # model that cannot do 'b' at all look healthy.
        y_true = ["a"] * 9 + ["b"]
        y_pred = ["a"] * 10

        assert metrics.accuracy(y_true, y_pred) == pytest.approx(0.9)
        assert metrics.macro_f1(y_true, y_pred, ("a", "b")) < 0.55


class TestReliabilityBins:
    def test_bins_partition_the_unit_interval(self) -> None:
        bins = metrics.reliability_bins([0.5], [True], n_bins=4)
        assert [(b.lower, b.upper) for b in bins] == [
            (0.0, 0.25),
            (0.25, 0.5),
            (0.5, 0.75),
            (0.75, 1.0),
        ]

    def test_confidence_of_exactly_one_is_counted(self) -> None:
        # The last bin must be closed at 1.0. A half-open final bin silently
        # drops every fully-confident prediction, which would understate ECE for
        # exactly the overconfident models we care about catching.
        bins = metrics.reliability_bins([1.0], [True], n_bins=10)
        assert sum(b.count for b in bins) == 1
        assert bins[-1].count == 1

    def test_boundary_value_lands_in_the_upper_bin(self) -> None:
        # 0.5 with 2 bins belongs to [0.5, 1.0], not [0.0, 0.5).
        bins = metrics.reliability_bins([0.5], [True], n_bins=2)
        assert bins[0].count == 0
        assert bins[1].count == 1

    def test_empty_bins_report_zero_rather_than_nan(self) -> None:
        bins = metrics.reliability_bins([0.9], [True], n_bins=10)
        empty = [b for b in bins if b.count == 0]
        assert empty
        assert all(b.accuracy == 0.0 and b.mean_confidence == 0.0 for b in empty)

    def test_out_of_range_confidence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            metrics.reliability_bins([1.5], [True], n_bins=10)

    def test_accuracy_and_mean_confidence_within_a_bin(self) -> None:
        # Four predictions all in [0.8, 0.9): confidences .80 .82 .84 .86,
        # three correct. mean confidence = 0.83, accuracy = 0.75.
        bins = metrics.reliability_bins(
            [0.80, 0.82, 0.84, 0.86], [True, True, True, False], n_bins=10
        )
        bin_eight = bins[8]
        assert bin_eight.count == 4
        assert bin_eight.mean_confidence == pytest.approx(0.83)
        assert bin_eight.accuracy == pytest.approx(0.75)


class TestExpectedCalibrationError:
    def test_perfectly_calibrated_is_zero(self) -> None:
        # Two predictions at confidence 1.0, both correct: |1.0 - 1.0| = 0.
        assert metrics.expected_calibration_error(
            [1.0, 1.0], [True, True], n_bins=10
        ) == pytest.approx(0.0)

    def test_maximally_overconfident_is_one(self) -> None:
        # Confidence 1.0 and always wrong: |0.0 - 1.0| = 1.0.
        assert metrics.expected_calibration_error(
            [1.0, 1.0], [False, False], n_bins=10
        ) == pytest.approx(1.0)

    def test_weighted_average_across_two_bins_by_hand(self) -> None:
        # Bin [0.0,0.1): one prediction, confidence 0.05, wrong.
        #   accuracy 0.0, gap |0.0 - 0.05| = 0.05, weight 1/3
        # Bin [0.9,1.0]: two predictions, confidences 0.9 and 1.0, both correct.
        #   accuracy 1.0, mean confidence 0.95, gap 0.05, weight 2/3
        # ECE = (1/3)(0.05) + (2/3)(0.05) = 0.05
        ece = metrics.expected_calibration_error(
            [0.05, 0.9, 1.0], [False, True, True], n_bins=10
        )
        assert ece == pytest.approx(0.05)

    def test_underconfidence_is_penalised_too(self) -> None:
        # ECE is symmetric: being right while claiming 0.6 is miscalibrated in
        # the same magnitude as being wrong while claiming 0.6.
        over = metrics.expected_calibration_error([0.6], [False], n_bins=10)
        under = metrics.expected_calibration_error([0.6], [True], n_bins=10)
        assert over == pytest.approx(0.6)
        assert under == pytest.approx(0.4)

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            metrics.expected_calibration_error([], [], n_bins=10)


class TestTopClassConfidence:
    def test_takes_row_maximum(self) -> None:
        proba = np.array([[0.1, 0.9], [0.7, 0.3]])
        assert metrics.top_class_confidence(proba).tolist() == [0.9, 0.7]

    def test_rejects_non_2d(self) -> None:
        with pytest.raises(ValueError, match="2-D"):
            metrics.top_class_confidence(np.array([0.5, 0.5]))


class TestEvaluate:
    def _proba(self, rows: list[list[float]]) -> NDArray[np.float64]:
        return np.array(rows, dtype=np.float64)

    def test_produces_the_documented_schema(self) -> None:
        result = metrics.evaluate(
            ["a", "b"],
            ["a", "b"],
            self._proba([[0.9, 0.05, 0.05], [0.1, 0.8, 0.1]]),
            LABELS,
            n_bins=10,
        )
        # These keys are a contract: the registry attaches this dict and the M5
        # gate reads macro_f1 and per_class[].f1 out of it.
        for key in (
            "macro_f1",
            "accuracy",
            "expected_calibration_error",
            "per_class",
            "reliability_bins",
            "confusion_matrix",
            "n_samples",
            "confidence_summary",
        ):
            assert key in result

    def test_rejects_proba_with_wrong_row_count(self) -> None:
        with pytest.raises(ValueError, match="rows"):
            metrics.evaluate(
                ["a", "b"], ["a", "b"], self._proba([[1.0, 0.0, 0.0]]), LABELS, n_bins=10
            )

    def test_rejects_proba_with_wrong_column_count(self) -> None:
        with pytest.raises(ValueError, match="columns"):
            metrics.evaluate(
                ["a"], ["a"], self._proba([[0.5, 0.5]]), LABELS, n_bins=10
            )

    def test_confusion_matrix_labels_match_requested_order(self) -> None:
        result = metrics.evaluate(
            ["a"], ["a"], self._proba([[1.0, 0.0, 0.0]]), LABELS, n_bins=10
        )
        assert result["confusion_matrix"]["labels"] == list(LABELS)
