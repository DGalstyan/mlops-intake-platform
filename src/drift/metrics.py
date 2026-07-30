"""Drift statistics: PSI, KS, and categorical distribution distance.

Pure functions over arrays, with no AWS, no file and no model dependency, for the
same reason the classification metrics are: the math is checkable against
hand-computed values. A drift number that nobody has verified is worse than no drift
number, because it will be believed.

Implemented directly rather than pulled from a library so the binning is under our
control. That matters more here than it looks: the baseline artifact stores its
histogram **edges**, and PSI must be computed against those same edges. A library
that re-derives bins from the data being tested would compare two differently-binned
distributions and manufacture drift out of nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Mapping, Sequence

import numpy as np
from numpy.typing import NDArray

# Conventional PSI interpretation bands. Widely used in credit-risk modelling and
# adopted here because they are a shared vocabulary, not because they are derived
# from this dataset — which is exactly why the report states them as thresholds
# rather than as truth.
PSI_STABLE: Final[float] = 0.10
PSI_MODERATE: Final[float] = 0.25

# Added to zero proportions before taking a log. A bin that is empty in one
# distribution and populated in the other yields an infinite PSI otherwise, and one
# empty tail bin would then dominate the whole statistic. 1e-6 is small enough not to
# distort populated bins and large enough to keep the result finite.
EPSILON: Final[float] = 1e-6


@dataclass(frozen=True, slots=True)
class DriftMeasure:
    """One drift statistic with its interpretation."""

    name: str
    statistic: float
    threshold: float
    breached: bool
    interpretation: str
    detail: dict[str, float] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "statistic": round(self.statistic, 6),
            "threshold": self.threshold,
            "breached": self.breached,
            "interpretation": self.interpretation,
        }
        if self.detail:
            payload["detail"] = {k: round(v, 6) for k, v in self.detail.items()}
        return payload


def _proportions(counts: Sequence[float]) -> NDArray[np.float64]:
    array = np.asarray(counts, dtype=np.float64)
    if np.any(array < 0):
        raise ValueError("counts must be non-negative")
    total = array.sum()
    if total <= 0:
        raise ValueError("cannot compute proportions from an empty distribution")
    proportions: NDArray[np.float64] = array / total
    return proportions


def population_stability_index(
    baseline_counts: Sequence[float], actual_counts: Sequence[float]
) -> float:
    """PSI between two binned distributions.

        PSI = sum over bins of (actual_pct - baseline_pct) * ln(actual_pct / baseline_pct)

    Symmetric in the sense that swapping the arguments gives the same value — the
    (a-b)*ln(a/b) term is invariant under exchange — which is worth knowing because
    it means PSI tells you *that* the distributions differ, never in which direction.
    The report pairs it with the raw means for that reason.

    Both inputs must already be binned against the SAME edges. This function cannot
    check that, which is why the baseline artifact stores its edges and the caller
    reuses them.
    """
    if len(baseline_counts) != len(actual_counts):
        raise ValueError(
            f"bin counts differ: baseline has {len(baseline_counts)} bins, "
            f"actual has {len(actual_counts)}. They must share the same edges."
        )
    if len(baseline_counts) < 2:
        raise ValueError("PSI needs at least 2 bins to be meaningful")

    baseline = _proportions(baseline_counts) + EPSILON
    actual = _proportions(actual_counts) + EPSILON

    return float(np.sum((actual - baseline) * np.log(actual / baseline)))


def interpret_psi(value: float) -> str:
    if value < PSI_STABLE:
        return "stable"
    if value < PSI_MODERATE:
        return "moderate shift"
    return "significant shift"


def psi_measure(
    name: str,
    baseline_counts: Sequence[float],
    actual_counts: Sequence[float],
    *,
    threshold: float = PSI_MODERATE,
) -> DriftMeasure:
    value = population_stability_index(baseline_counts, actual_counts)
    return DriftMeasure(
        name=name,
        statistic=value,
        threshold=threshold,
        breached=value >= threshold,
        interpretation=interpret_psi(value),
    )


def ks_statistic(baseline: Sequence[float], actual: Sequence[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic: max |F_baseline(x) - F_actual(x)|.

    Computed from the empirical CDFs directly rather than via a library, so the
    binning question does not arise at all — KS is distribution-free and needs no
    bins, which makes it the right complement to PSI. PSI can miss a shift that
    happens *within* a bin; KS cannot, because it has no bins to hide in.

    Returns the statistic only, not a p-value. At production sample sizes a KS test
    rejects the null for differences far too small to act on, so the p-value would be
    significant almost always and would not help anyone decide anything.
    """
    if not len(baseline) or not len(actual):
        raise ValueError("KS needs a non-empty sample on both sides")

    a = np.sort(np.asarray(baseline, dtype=np.float64))
    b = np.sort(np.asarray(actual, dtype=np.float64))

    # Evaluate both empirical CDFs on the union of observed values.
    grid = np.concatenate([a, b])
    cdf_a = np.searchsorted(a, grid, side="right") / a.size
    cdf_b = np.searchsorted(b, grid, side="right") / b.size
    return float(np.max(np.abs(cdf_a - cdf_b)))


def ks_measure(
    name: str,
    baseline: Sequence[float],
    actual: Sequence[float],
    *,
    threshold: float = 0.2,
) -> DriftMeasure:
    value = ks_statistic(baseline, actual)
    return DriftMeasure(
        name=name,
        statistic=value,
        threshold=threshold,
        breached=value >= threshold,
        interpretation=(
            "distributions are close"
            if value < threshold
            else "distributions differ materially"
        ),
        detail={
            "baseline_mean": float(np.mean(baseline)),
            "actual_mean": float(np.mean(actual)),
        },
    )


def categorical_psi(
    baseline_proportions: Mapping[str, float],
    actual_counts: Mapping[str, int],
) -> tuple[float, dict[str, float]]:
    """PSI over a categorical distribution, e.g. predicted-class mix.

    Returns the total and the per-category contribution, because "prediction drift is
    0.31" is not actionable while "0.28 of the 0.31 comes from `invoice`" is.
    """
    categories = sorted(set(baseline_proportions) | set(actual_counts))
    if not categories:
        raise ValueError("no categories to compare")

    total_actual = sum(actual_counts.values())
    if total_actual <= 0:
        raise ValueError("actual distribution is empty")

    contributions: dict[str, float] = {}
    for category in categories:
        expected = float(baseline_proportions.get(category, 0.0)) + EPSILON
        observed = (actual_counts.get(category, 0) / total_actual) + EPSILON
        contributions[category] = float((observed - expected) * math.log(observed / expected))

    return float(sum(contributions.values())), contributions


def bin_samples(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    """Bin values against the baseline's stored edges.

    The edges come from the baseline artifact and are never recomputed from the data
    being tested. Recomputing them is the single most common way to produce drift
    numbers that mean nothing — two differently-binned distributions always differ.
    """
    if len(edges) < 2:
        raise ValueError("need at least two edges to form a bin")
    counts, _ = np.histogram(np.asarray(values, dtype=np.float64), bins=list(edges))
    return [int(c) for c in counts]


def relative_change(baseline: float, actual: float) -> float:
    """Signed relative change, safe at zero.

    Used for the concept-drift proxies, where the DIRECTION is the whole point — a
    falling override rate and a rising one mean opposite things, and PSI would report
    both as simply "different".
    """
    if baseline == 0:
        return 0.0 if actual == 0 else float("inf")
    return (actual - baseline) / abs(baseline)
