"""Classification and calibration metrics.

Deliberately pure functions over arrays, with no model, file or AWS dependency,
for two reasons:

1. The math is unit-testable against hand-computed values. Metric code that is
   only exercised through a training job is metric code nobody has checked.
2. The M5 retrain gate must compare candidate against champion using the *same*
   computation, on the same golden set. Forking the metric logic between "the
   number we report" and "the number we gate on" is how a gate silently stops
   meaning anything.

`macro_f1` is the headline number because the classes are balanced by
construction in the golden set and macro-averaging refuses to let a strong
majority class hide a collapsed minority one — which is exactly the failure the
per-class floor in the gate exists to catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    """Per-class precision / recall / F1 and support."""

    label: str
    precision: float
    recall: float
    f1: float
    support: int


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bin of a reliability diagram.

    `mean_confidence` vs `accuracy` is the calibration signal: a perfectly
    calibrated model has them equal in every populated bin.
    """

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float


def _validate_lengths(y_true: Sequence[str], y_pred: Sequence[str]) -> None:
    if len(y_true) != len(y_pred):
        raise ValueError(
            f"y_true and y_pred must be the same length, got "
            f"{len(y_true)} and {len(y_pred)}"
        )
    if not y_true:
        raise ValueError("cannot compute metrics on an empty dataset")


def confusion_matrix(
    y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str]
) -> NDArray[np.int64]:
    """Rows are true classes, columns predicted, both in `labels` order."""
    _validate_lengths(y_true, y_pred)
    index = {label: i for i, label in enumerate(labels)}
    matrix = np.zeros((len(labels), len(labels)), dtype=np.int64)
    for truth, prediction in zip(y_true, y_pred, strict=False):
        if truth not in index:
            raise ValueError(f"y_true contains unknown label {truth!r}")
        if prediction not in index:
            raise ValueError(f"y_pred contains unknown label {prediction!r}")
        matrix[index[truth], index[prediction]] += 1
    return matrix


def per_class_metrics(
    y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str]
) -> list[ClassMetrics]:
    """Precision, recall and F1 for each label.

    A class with no predictions gets precision 0, and a class with no true
    instances gets recall 0, rather than NaN. Returning NaN would propagate into
    macro-F1 and make the gate comparison undefined exactly when a class has
    collapsed — the case the gate most needs to catch.
    """
    matrix = confusion_matrix(y_true, y_pred, labels)
    results: list[ClassMetrics] = []
    for i, label in enumerate(labels):
        true_positive = int(matrix[i, i])
        predicted = int(matrix[:, i].sum())
        actual = int(matrix[i, :].sum())

        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / actual if actual else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        results.append(
            ClassMetrics(
                label=label,
                precision=precision,
                recall=recall,
                f1=f1,
                support=actual,
            )
        )
    return results


def macro_f1(
    y_true: Sequence[str], y_pred: Sequence[str], labels: Sequence[str]
) -> float:
    """Unweighted mean of per-class F1 — every class counts equally."""
    scores = [m.f1 for m in per_class_metrics(y_true, y_pred, labels)]
    return float(np.mean(scores)) if scores else 0.0


def accuracy(y_true: Sequence[str], y_pred: Sequence[str]) -> float:
    _validate_lengths(y_true, y_pred)
    correct = sum(1 for t, p in zip(y_true, y_pred, strict=False) if t == p)
    return correct / len(y_true)


def reliability_bins(
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    n_bins: int,
) -> list[ReliabilityBin]:
    """Bucket predictions into `n_bins` equal-width confidence bins.

    Bins are half-open [lower, upper) except the last, which includes 1.0 — so a
    prediction with confidence exactly 1.0 is counted rather than silently
    dropped.
    """
    if len(confidences) != len(correct):
        raise ValueError(
            f"confidences and correct must be the same length, got "
            f"{len(confidences)} and {len(correct)}"
        )
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")
    if not confidences:
        raise ValueError("cannot compute reliability bins on an empty dataset")

    conf = np.asarray(confidences, dtype=np.float64)
    if np.any((conf < 0.0) | (conf > 1.0)):
        raise ValueError("confidences must all lie in [0, 1]")
    hit = np.asarray(correct, dtype=bool)

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lower, upper = float(edges[i]), float(edges[i + 1])
        if i == n_bins - 1:
            mask = (conf >= lower) & (conf <= upper)
        else:
            mask = (conf >= lower) & (conf < upper)
        count = int(mask.sum())
        bins.append(
            ReliabilityBin(
                lower=lower,
                upper=upper,
                count=count,
                mean_confidence=float(conf[mask].mean()) if count else 0.0,
                accuracy=float(hit[mask].mean()) if count else 0.0,
            )
        )
    return bins


def expected_calibration_error(
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    n_bins: int,
) -> float:
    """Weighted average gap between confidence and accuracy across bins.

    ECE = sum over bins of (bin_count / total) * |accuracy - mean_confidence|.

    0 is perfect calibration. This is the number that decides whether the Route
    state's confidence threshold means anything: an accurate but badly
    calibrated model still routes documents wrongly, because the threshold is
    compared against a probability that does not correspond to a real
    likelihood of being right.

    Empty bins contribute nothing — they carry zero weight, which is why they
    are not an error case.
    """
    bins = reliability_bins(confidences, correct, n_bins=n_bins)
    total = sum(b.count for b in bins)
    if total == 0:
        raise ValueError("no predictions to compute ECE over")
    return float(
        sum(
            (b.count / total) * abs(b.accuracy - b.mean_confidence)
            for b in bins
            if b.count
        )
    )


def top_class_confidence(proba: NDArray[np.float64]) -> NDArray[np.float64]:
    """Highest probability per row — the number the Route state gates on."""
    if proba.ndim != 2:
        raise ValueError(f"expected a 2-D probability array, got shape {proba.shape}")
    return np.asarray(proba.max(axis=1), dtype=np.float64)


def evaluate(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    proba: NDArray[np.float64],
    labels: Sequence[str],
    *,
    n_bins: int,
) -> dict[str, Any]:
    """Compute the full metric set in the stable schema artifacts carry.

    This dict shape is a contract: the registry attaches it as ModelMetrics, and
    the M5 gate reads `macro_f1` and `per_class[].f1` out of it. Adding keys is
    safe; renaming or removing these is not.
    """
    _validate_lengths(y_true, y_pred)
    if proba.shape[0] != len(y_true):
        raise ValueError(
            f"proba has {proba.shape[0]} rows but there are {len(y_true)} labels"
        )
    if proba.shape[1] != len(labels):
        raise ValueError(
            f"proba has {proba.shape[1]} columns but there are {len(labels)} labels"
        )

    confidences = top_class_confidence(proba)
    correct = [t == p for t, p in zip(y_true, y_pred, strict=False)]

    return {
        "macro_f1": macro_f1(y_true, y_pred, labels),
        "accuracy": accuracy(y_true, y_pred),
        "expected_calibration_error": expected_calibration_error(
            confidences.tolist(), correct, n_bins=n_bins
        ),
        "per_class": [asdict(m) for m in per_class_metrics(y_true, y_pred, labels)],
        "reliability_bins": [
            asdict(b)
            for b in reliability_bins(confidences.tolist(), correct, n_bins=n_bins)
        ],
        "confusion_matrix": {
            "labels": list(labels),
            "rows_are_true_classes": True,
            "matrix": confusion_matrix(y_true, y_pred, labels).tolist(),
        },
        "n_samples": len(y_true),
        "confidence_summary": {
            "mean": float(confidences.mean()),
            "p10": float(np.percentile(confidences, 10)),
            "p50": float(np.percentile(confidences, 50)),
            "p90": float(np.percentile(confidences, 90)),
        },
    }
