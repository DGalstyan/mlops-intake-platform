"""Baseline statistics artifact — the reference distributions drift is measured against.

This artifact is a **contract with M5**, which is why its schema is versioned and
why a consumer should refuse a version it does not recognise. M1 writes it; the
scheduled drift job reads it and compares production traffic against it.

What belongs in it, and why each piece earns its place (the README repeats this
justification, because the assignment asks for it explicitly):

- **Prediction priors** — the per-class rate the model predicted on a reference
  set. Prediction drift is a shift in this distribution. Cheap to compute, and
  the only drift signal available when no ground truth exists, which is the
  normal production case.
- **Document length and token-count distributions** — quantiles plus a fixed-edge
  histogram. Input drift usually shows up here first and needs no model at all,
  so it still works if the endpoint is down. Fixed edges are stored with the
  artifact so production bins are comparable; recomputing edges from live data
  would compare two differently-binned distributions and manufacture drift.
- **Confidence histogram** — the calibration reference. If confidence decays
  while inputs and predictions look stable, that is the concept-drift proxy: the
  world changed in a way the features do not capture.
- **Top-feature summary with TF-IDF means and variances** — per-feature moments
  let the drift job compute PSI on individual features and attribute drift to
  specific vocabulary rather than only reporting that "something moved". Capped
  to the top N by mean weight, because storing 20,000 features would make the
  artifact large and the tail is noise.
- **Vocabulary coverage** — the fraction of production tokens present in the
  training vocabulary. A TF-IDF model silently ignores unseen tokens, so falling
  coverage means the model is increasingly blind to its input while its
  confidence stays high. Nothing else in the artifact would reveal that.

Deliberately NOT in it: accuracy or F1. Those are properties of a model
evaluated against labels, and they live in `metrics.json`. Mixing them in here
invites the mistake M5 must avoid — treating "the data changed" and "the model
got worse" as one signal.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import BASELINE_SCHEMA_VERSION, CALIBRATION_BINS  # noqa: E402

# Fixed histogram edges, stored in the artifact. Chosen to bracket the generated
# corpus with headroom either side so production documents longer or shorter than
# anything seen in training still land in a real bin rather than being clipped.
LENGTH_HISTOGRAM_EDGES: Final[tuple[int, ...]] = (
    0,
    100,
    200,
    400,
    600,
    900,
    1300,
    1800,
    2500,
    4000,
    1_000_000,
)

TOKEN_HISTOGRAM_EDGES: Final[tuple[int, ...]] = (
    0,
    20,
    40,
    70,
    100,
    150,
    220,
    320,
    480,
    700,
    1_000_000,
)

# How many TF-IDF features to keep moments for.
TOP_FEATURE_COUNT: Final[int] = 200


@dataclass(frozen=True, slots=True)
class Distribution:
    """Quantile summary plus a fixed-edge histogram of a numeric quantity."""

    count: int
    mean: float
    std: float
    min: float
    p10: float
    p50: float
    p90: float
    max: float
    histogram_edges: list[float]
    histogram_counts: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": self.mean,
            "std": self.std,
            "min": self.min,
            "p10": self.p10,
            "p50": self.p50,
            "p90": self.p90,
            "max": self.max,
            "histogram_edges": self.histogram_edges,
            "histogram_counts": self.histogram_counts,
        }


def summarise(values: Sequence[float], edges: Sequence[float]) -> Distribution:
    """Summarise a numeric sample against fixed histogram edges."""
    if not values:
        raise ValueError("cannot summarise an empty sample")
    array = np.asarray(values, dtype=np.float64)
    counts, _ = np.histogram(array, bins=list(edges))
    return Distribution(
        count=int(array.size),
        mean=float(array.mean()),
        std=float(array.std(ddof=0)),
        min=float(array.min()),
        p10=float(np.percentile(array, 10)),
        p50=float(np.percentile(array, 50)),
        p90=float(np.percentile(array, 90)),
        max=float(array.max()),
        histogram_edges=[float(e) for e in edges],
        histogram_counts=[int(c) for c in counts],
    )


def _prediction_priors(
    predictions: Sequence[str], labels: Sequence[str]
) -> dict[str, float]:
    total = len(predictions)
    if total == 0:
        raise ValueError("cannot compute priors with no predictions")
    counts = {label: 0 for label in labels}
    for prediction in predictions:
        if prediction not in counts:
            raise ValueError(f"prediction {prediction!r} is not a known class")
        counts[prediction] += 1
    return {label: counts[label] / total for label in labels}


def _feature_moments(
    matrix: Any, feature_names: Sequence[str], *, top_n: int
) -> list[dict[str, Any]]:
    """Per-feature mean and variance for the top `top_n` features by mean weight.

    Accepts the sparse matrix a TF-IDF vectorizer produces. Mean and variance are
    computed without densifying the whole matrix, so this stays cheap even at
    20,000 features: E[x^2] - E[x]^2 from the sparse sums.
    """
    n_rows = matrix.shape[0]
    if n_rows == 0:
        raise ValueError("cannot compute feature moments on an empty matrix")

    sums = np.asarray(matrix.sum(axis=0)).ravel()
    squared = matrix.copy()
    squared.data = squared.data**2
    sums_squared = np.asarray(squared.sum(axis=0)).ravel()

    means = sums / n_rows
    variances = np.maximum(sums_squared / n_rows - means**2, 0.0)

    top_indices = np.argsort(means)[::-1][:top_n]
    return [
        {
            "feature": str(feature_names[i]),
            "mean": float(means[i]),
            "variance": float(variances[i]),
        }
        for i in top_indices
    ]


def build_baseline(
    *,
    texts: Sequence[str],
    predictions: Sequence[str],
    confidences: Sequence[float],
    labels: Sequence[str],
    snapshot_id: str,
    git_sha: str,
    feature_matrix: Any | None = None,
    feature_names: Sequence[str] | None = None,
    holdout_confidences: Sequence[float] | None = None,
    n_confidence_bins: int = CALIBRATION_BINS,
) -> dict[str, Any]:
    """Assemble the baseline artifact.

    `texts` should be the *reference* set the drift job compares against — the
    training split, not the golden set. Using the golden set would make the
    baseline describe 240 documents the model never saw, which is a worse
    description of "normal" than the data it actually learned from.

    `holdout_confidences` is the exception, and must come from the GOLDEN set. See
    the module docstring: a training-set confidence reference bakes in the
    memorisation gap and makes every production window look decayed.
    """
    if not (len(texts) == len(predictions) == len(confidences)):
        raise ValueError(
            "texts, predictions and confidences must be the same length, got "
            f"{len(texts)}, {len(predictions)}, {len(confidences)}"
        )

    char_lengths = [float(len(t)) for t in texts]
    token_counts = [float(len(t.split())) for t in texts]

    vocabulary_size = (
        len(feature_names) if feature_names is not None else None
    )

    artifact: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "lineage": {
            "data_snapshot_id": snapshot_id,
            "git_sha": git_sha,
        },
        "classes": list(labels),
        "reference_set_size": len(texts),
        # --- prediction drift reference ---
        "prediction_priors": _prediction_priors(predictions, labels),
        # --- input drift reference ---
        "document_char_length": summarise(
            char_lengths, LENGTH_HISTOGRAM_EDGES
        ).to_dict(),
        "document_token_count": summarise(
            token_counts, TOKEN_HISTOGRAM_EDGES
        ).to_dict(),
        # --- calibration / concept-drift-proxy reference ---
        # Held-out where available. `confidence_source` records which, so a drift job
        # reading an older artifact can tell whether the reference is trustworthy
        # rather than silently comparing against an inflated one.
        "confidence": summarise(
            list(holdout_confidences if holdout_confidences else confidences),
            [i / n_confidence_bins for i in range(n_confidence_bins + 1)],
        ).to_dict(),
        "confidence_source": "golden_holdout" if holdout_confidences else "train",
        "confidence_train_reference": summarise(
            list(confidences),
            [i / n_confidence_bins for i in range(n_confidence_bins + 1)],
        ).to_dict(),
        "vocabulary_size": vocabulary_size,
    }

    if feature_matrix is not None and feature_names is not None:
        artifact["top_features"] = _feature_moments(
            feature_matrix, feature_names, top_n=TOP_FEATURE_COUNT
        )
        artifact["top_feature_count"] = TOP_FEATURE_COUNT
    else:
        # Recorded explicitly rather than omitted: a drift job that expects
        # per-feature moments should be able to tell "not computed" from
        # "computed and empty".
        artifact["top_features"] = None
        artifact["top_feature_count"] = 0

    return artifact


def load_baseline(path: Path) -> dict[str, Any]:
    """Read a baseline artifact, refusing an unknown schema version.

    Failing closed on the major version is the point of versioning it: a drift
    job that silently reads a shape it does not understand reports drift numbers
    computed against the wrong fields, which is worse than not running.
    """
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version")
    if not isinstance(version, str):
        raise ValueError(f"baseline at {path} has no schema_version")
    major = version.split(".")[0]
    expected_major = BASELINE_SCHEMA_VERSION.split(".")[0]
    if major != expected_major:
        raise ValueError(
            f"baseline at {path} has schema_version {version}, which this code "
            f"(expecting {expected_major}.x.x) cannot read. Regenerate the "
            "baseline or pin the reader to the matching version."
        )
    return payload
