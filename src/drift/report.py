"""Drift report: three signal families, and the distinction between them.

The assignment asks for the "data changed" vs "model got worse" distinction and for
an explanation of why conflating them is a bug. The short version, which the report
states in its own output:

**The response differs, and one of the responses is actively harmful.**

- Inputs moved, model still coping (override rate and confidence steady) → the world
  changed and the model generalised. Retraining here spends money to fit the new
  distribution's noise, and worse, it retrains on *review-sourced* labels that are
  biased toward hard cases. You can make the model worse by responding to a signal
  that did not require a response.
- Inputs steady, model decaying (override rate rising, confidence falling) → the
  relationship changed underneath a stable distribution. This is the case that
  justifies retraining, and it is invisible to input-drift tests.
- Both → highest priority, and the only case where "retrain immediately" is the
  obvious answer.
- Neither → no action. Reporting "no drift" is a result, not a non-event.

A single "drift score" collapsing all of this would map two opposite situations onto
the same number and prescribe the same action for both. That is the bug.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np

from src.config import (
    AUTO_APPROVE_CONFIDENCE_THRESHOLD,
    BASELINE_SCHEMA_VERSION,
    DOCUMENT_CLASSES,
)
from src.drift.metrics import (
    DriftMeasure,
    bin_samples,
    categorical_psi,
    psi_measure,
    relative_change,
)

DRIFT_REPORT_SCHEMA_VERSION: Final[str] = "1.0.0"

# How much worse the concept proxies must get before they count as decay. Deliberately
# a *relative* change against the baseline rather than an absolute level, because the
# absolute override rate is meaningless — it is measured on a slice selected for being
# hard. Only the change is informative.
OVERRIDE_RATE_INCREASE_THRESHOLD: Final[float] = 0.50  # +50% relative
CONFIDENCE_DECAY_THRESHOLD: Final[float] = 0.10        # -10% relative on p10


class Verdict:
    """The four cases. Named so the report and the retrain trigger agree."""

    NO_DRIFT = "NO_DRIFT"
    DATA_CHANGED = "DATA_CHANGED"
    MODEL_DECAYED = "MODEL_DECAYED"
    BOTH = "DATA_CHANGED_AND_MODEL_DECAYED"


RECOMMENDED_ACTION: Final[dict[str, str]] = {
    Verdict.NO_DRIFT: (
        "No action. Inputs and model behaviour both look like the baseline."
    ),
    Verdict.DATA_CHANGED: (
        "DO NOT retrain on this signal alone. The input distribution moved but the "
        "model is still handling it — override rate and confidence are steady. "
        "Retraining here fits the new distribution's noise using review-sourced "
        "labels that are biased toward hard cases, which can make the model worse. "
        "Investigate what changed upstream (a new sender, a new scanner, a layout "
        "change) and consider whether the extraction schema or prompt needs updating. "
        "Raise the watch frequency; re-evaluate if the concept proxies start moving."
    ),
    Verdict.MODEL_DECAYED: (
        "RETRAIN CANDIDATE. Inputs look unchanged but the model is getting the "
        "reviewed slice wrong more often and/or is less confident. The relationship "
        "moved underneath a stable distribution, which is precisely what input-drift "
        "tests cannot see. Trigger the retrain state machine; the gate decides "
        "whether the candidate is actually better."
    ),
    Verdict.BOTH: (
        "RETRAIN CANDIDATE, HIGH PRIORITY. The distribution moved AND performance "
        "fell with it. Retrain, but read the sampling-bias section first: the "
        "corrections available to retrain on come from the low-confidence slice, so "
        "they under-represent exactly the new-distribution documents the model is "
        "still confident about."
    ),
}


@dataclass
class SignalGroup:
    """One family of drift signals."""

    family: str
    measures: list[DriftMeasure] = field(default_factory=list)

    @property
    def breached(self) -> bool:
        return any(m.breached for m in self.measures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "breached": self.breached,
            "measures": [m.to_dict() for m in self.measures],
        }


@dataclass
class ProductionWindow:
    """What the drift job observed. Assembled from data capture plus the results and
    corrections tables — deliberately not from the model, so input drift can still be
    computed when the endpoint is down."""

    texts: Sequence[str]
    predicted_classes: Sequence[str]
    confidences: Sequence[float]
    reviewed_count: int = 0
    override_count: int = 0
    schema_failure_count: int = 0

    def __post_init__(self) -> None:
        if not (len(self.texts) == len(self.predicted_classes) == len(self.confidences)):
            raise ValueError(
                "texts, predicted_classes and confidences must be the same length, "
                f"got {len(self.texts)}, {len(self.predicted_classes)}, "
                f"{len(self.confidences)}"
            )
        if self.override_count > self.reviewed_count:
            raise ValueError(
                f"override_count ({self.override_count}) exceeds reviewed_count "
                f"({self.reviewed_count})"
            )

    @property
    def size(self) -> int:
        return len(self.texts)

    @property
    def override_rate(self) -> float:
        """Overrides as a share of documents actually REVIEWED.

        Not of all documents. Using all documents would make this fall whenever
        auto-approval rose, for reasons unconnected to model quality.
        """
        return self.override_count / self.reviewed_count if self.reviewed_count else 0.0


def _baseline_override_rate(baseline: Mapping[str, Any]) -> float | None:
    """The baseline's own override rate, if it recorded one.

    M1's baseline is built from training data, where nothing was reviewed, so this is
    normally absent — and that absence is meaningful rather than an error. It means
    the first drift run has no override-rate reference and must say so instead of
    silently comparing against zero, which would report infinite decay on the first
    human correction.
    """
    value = baseline.get("override_rate_reference")
    return float(value) if isinstance(value, (int, float)) else None


def compute_input_drift(
    baseline: Mapping[str, Any], window: ProductionWindow
) -> SignalGroup:
    """Has the incoming data changed? Needs no model, so it works when the endpoint is down."""
    group = SignalGroup(family="input")

    char_lengths = [float(len(t)) for t in window.texts]
    token_counts = [float(len(t.split())) for t in window.texts]

    for key, values, label in (
        ("document_char_length", char_lengths, "document length (characters)"),
        ("document_token_count", token_counts, "document length (tokens)"),
    ):
        reference = baseline.get(key)
        if not isinstance(reference, dict):
            continue

        # Bin against the BASELINE's stored edges. Recomputing them from the window
        # would compare two differently-binned distributions and invent drift.
        edges = reference["histogram_edges"]
        group.measures.append(
            psi_measure(
                f"psi_{key}",
                reference["histogram_counts"],
                bin_samples(values, edges),
            )
        )

        # QUANTILE SHIFT alongside PSI, not KS.
        #
        # KS was the obvious choice and is wrong here. KS compares two empirical
        # CDFs, so it needs samples on both sides — and the baseline stores a
        # histogram, by design (raw samples would mean carrying document content into
        # a file a scheduled job reads). Reconstructing samples from the histogram to
        # run KS measures the RECONSTRUCTION, not the data: on an unshifted control
        # window it reported 0.30 and 0.38 ("breached") while PSI on the identical
        # data reported 0.009 and 0.002 ("stable"). Spreading the reconstruction
        # uniformly within bins rather than at midpoints roughly halved the error and
        # still left a false positive on token counts, because they are small integers
        # and no within-bin reconstruction recovers that.
        #
        # PSI is the right statistic for binned reference data and is kept. Quantile
        # shift adds what PSI cannot give: DIRECTION. PSI's (a-b)ln(a/b) term is
        # symmetric, so it says distributions differ but never which way — and
        # "documents got longer" and "documents got shorter" have different causes.
        quantile_shift = _quantile_shift(reference, values)
        if quantile_shift is not None:
            group.measures.append(
                DriftMeasure(
                    name=f"median_shift_{key}",
                    statistic=quantile_shift.statistic,
                    threshold=quantile_shift.threshold,
                    breached=quantile_shift.breached,
                    interpretation=f"{label}: {quantile_shift.interpretation}",
                    detail=quantile_shift.detail,
                )
            )

    return group


def _quantile_shift(
    reference: Mapping[str, Any], values: Sequence[float]
) -> DriftMeasure | None:
    """Relative movement of the median, using only quantiles the baseline stores.

    Reported alongside PSI to supply direction, which PSI structurally cannot: its
    per-bin term is symmetric under exchange, so a distribution that shifted left and
    one that shifted right produce the same number.

    Thresholded at 25% relative movement of the median. That is deliberately loose —
    this measure exists to say *which way*, and PSI is the one that decides whether
    the shift is large enough to matter.
    """
    if not values:
        return None
    baseline_p50 = float(reference.get("p50", 0.0))
    if baseline_p50 <= 0:
        return None

    actual_p50 = float(np.percentile(values, 50))
    change = relative_change(baseline_p50, actual_p50)
    direction = "longer" if change > 0 else "shorter"
    return DriftMeasure(
        name="median_shift",
        statistic=change,
        threshold=0.25,
        breached=abs(change) >= 0.25,
        interpretation=(
            f"median moved {change:+.1%} ({baseline_p50:.0f} -> {actual_p50:.0f}); "
            f"documents got {direction}"
        ),
        detail={"baseline_p50": baseline_p50, "actual_p50": actual_p50},
    )


def compute_prediction_drift(
    baseline: Mapping[str, Any], window: ProductionWindow
) -> SignalGroup:
    """Has the output mix changed?

    Benign on its own — if the input mix changed, the prediction mix should follow.
    It only becomes evidence of a problem alongside the concept proxies, which is the
    whole reason these are separate families.
    """
    group = SignalGroup(family="prediction")

    priors = baseline.get("prediction_priors")
    if not isinstance(priors, dict):
        return group

    counts: dict[str, int] = {label: 0 for label in DOCUMENT_CLASSES}
    for predicted in window.predicted_classes:
        counts[predicted] = counts.get(predicted, 0) + 1

    total, contributions = categorical_psi(priors, counts)
    group.measures.append(
        DriftMeasure(
            name="psi_predicted_class_mix",
            statistic=total,
            threshold=0.25,
            breached=total >= 0.25,
            interpretation=(
                "class mix matches the baseline"
                if total < 0.25
                else "class mix has shifted; largest contributor: "
                + max(contributions, key=lambda k: abs(contributions[k]))
            ),
            detail=contributions,
        )
    )
    return group


def compute_concept_drift(
    baseline: Mapping[str, Any], window: ProductionWindow
) -> SignalGroup:
    """Has the RELATIONSHIP changed — i.e. did the model get worse?

    Two proxies, because there is no ground truth in production:
      1. override-rate trend  — reviewers correcting more often
      2. confidence decay     — the model itself less certain

    Both are proxies and both are stated as such in the report. Neither can see a
    confidently-wrong document, which is the blind spot the sampling-bias section is
    about.
    """
    group = SignalGroup(family="concept")

    # --- confidence decay, against the baseline's confidence histogram ---
    reference = baseline.get("confidence")
    if isinstance(reference, dict):
        baseline_p10 = float(reference.get("p10", 0.0))
        actual_p10 = float(np.percentile(window.confidences, 10)) if window.size else 0.0
        change = relative_change(baseline_p10, actual_p10)
        group.measures.append(
            DriftMeasure(
                name="confidence_p10_decay",
                statistic=change,
                threshold=-CONFIDENCE_DECAY_THRESHOLD,
                # Only a FALL counts. A rise in confidence is not decay, and treating
                # a two-sided test as decay would fire on a model that got better.
                breached=change <= -CONFIDENCE_DECAY_THRESHOLD,
                interpretation=(
                    f"p10 confidence moved {change:+.1%} vs baseline "
                    f"({baseline_p10:.3f} -> {actual_p10:.3f})"
                ),
                detail={"baseline_p10": baseline_p10, "actual_p10": actual_p10},
            )
        )

        # The share below the auto-approve threshold is the business-visible form of
        # the same signal: it is exactly the extra human work created.
        below = sum(
            1 for c in window.confidences if c < AUTO_APPROVE_CONFIDENCE_THRESHOLD
        )
        share_below = below / window.size if window.size else 0.0
        group.measures.append(
            DriftMeasure(
                name="share_below_auto_approve_threshold",
                statistic=share_below,
                threshold=0.30,
                breached=share_below >= 0.30,
                interpretation=(
                    f"{share_below:.1%} of documents fell below the "
                    f"{AUTO_APPROVE_CONFIDENCE_THRESHOLD} auto-approve threshold"
                ),
            )
        )

    # --- override-rate trend ---
    baseline_override = _baseline_override_rate(baseline)
    if baseline_override is None:
        group.measures.append(
            DriftMeasure(
                name="override_rate_trend",
                statistic=window.override_rate,
                threshold=OVERRIDE_RATE_INCREASE_THRESHOLD,
                # NOT breached: no reference to compare against. Reporting a breach
                # here would fire on the first human correction of a new deployment,
                # because the baseline is built from training data where nothing was
                # reviewed.
                breached=False,
                interpretation=(
                    f"override rate is {window.override_rate:.1%} of "
                    f"{window.reviewed_count} reviewed documents, but the baseline "
                    "records no override-rate reference (it was built from training "
                    "data, where nothing was reviewed). This becomes a usable signal "
                    "once a production window has been captured as the new reference."
                ),
            )
        )
    else:
        change = relative_change(baseline_override, window.override_rate)
        group.measures.append(
            DriftMeasure(
                name="override_rate_trend",
                statistic=change,
                threshold=OVERRIDE_RATE_INCREASE_THRESHOLD,
                breached=change >= OVERRIDE_RATE_INCREASE_THRESHOLD,
                interpretation=(
                    f"override rate moved {change:+.1%} "
                    f"({baseline_override:.1%} -> {window.override_rate:.1%})"
                ),
                detail={
                    "baseline_rate": baseline_override,
                    "actual_rate": window.override_rate,
                    "reviewed_count": float(window.reviewed_count),
                },
            )
        )

    return group


def classify(
    input_drift: SignalGroup,
    prediction_drift: SignalGroup,
    concept_drift: SignalGroup,
) -> str:
    """The required distinction.

    Prediction drift is deliberately NOT treated as evidence on its own. If the input
    mix changed, the prediction mix should follow — that is the model working, not
    failing. It is recorded, and it sharpens the input verdict, but it cannot by
    itself make something look like decay.
    """
    data_changed = input_drift.breached or prediction_drift.breached
    model_decayed = concept_drift.breached

    if data_changed and model_decayed:
        return Verdict.BOTH
    if model_decayed:
        return Verdict.MODEL_DECAYED
    if data_changed:
        return Verdict.DATA_CHANGED
    return Verdict.NO_DRIFT


def build_report(
    *,
    baseline: Mapping[str, Any],
    window: ProductionWindow,
    window_label: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the full drift report."""
    version = str(baseline.get("schema_version", ""))
    if version.split(".")[0] != BASELINE_SCHEMA_VERSION.split(".")[0]:
        raise ValueError(
            f"baseline schema_version {version!r} is not readable by this drift job "
            f"(expects {BASELINE_SCHEMA_VERSION.split('.')[0]}.x.x). Refusing to "
            "compute drift against a shape it may misread."
        )

    input_drift = compute_input_drift(baseline, window)
    prediction_drift = compute_prediction_drift(baseline, window)
    concept_drift = compute_concept_drift(baseline, window)
    verdict = classify(input_drift, prediction_drift, concept_drift)

    return {
        "schema_version": DRIFT_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at
        or datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "window": {
            "label": window_label,
            "documents": window.size,
            "reviewed": window.reviewed_count,
            "overrides": window.override_count,
            "schema_failures": window.schema_failure_count,
        },
        "baseline": {
            "schema_version": version,
            "data_snapshot_id": baseline.get("lineage", {}).get("data_snapshot_id"),
            "git_sha": baseline.get("lineage", {}).get("git_sha"),
            "reference_set_size": baseline.get("reference_set_size"),
        },
        "verdict": verdict,
        "recommended_action": RECOMMENDED_ACTION[verdict],
        "why_the_distinction_matters": (
            "Input drift and model decay require different responses, and one of them "
            "is actively harmful when applied to the other. Retraining in response to "
            "input drift alone spends money fitting the new distribution's noise, "
            "using labels sourced from human review — which only sees low-confidence "
            "documents and is therefore biased toward hard cases. A single combined "
            "'drift score' would map these opposite situations onto the same number "
            "and prescribe the same action for both."
        ),
        "signals": {
            "input": input_drift.to_dict(),
            "prediction": prediction_drift.to_dict(),
            "concept": concept_drift.to_dict(),
        },
        "should_trigger_retrain": verdict in (Verdict.MODEL_DECAYED, Verdict.BOTH),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """Human-readable form. The JSON is for machines; this is what a person reads."""
    verdict = str(report["verdict"])
    window = report["window"]
    lines = [
        "# Drift report",
        "",
        f"**Verdict: {verdict}**",
        "",
        f"- Window: `{window['label']}` — {window['documents']} documents, "
        f"{window['reviewed']} reviewed, {window['overrides']} overridden",
        f"- Baseline snapshot: `{report['baseline']['data_snapshot_id']}`",
        f"- Generated: {report['generated_at']}",
        f"- Triggers retrain: **{'yes' if report['should_trigger_retrain'] else 'no'}**",
        "",
        "## Recommended action",
        "",
        str(report["recommended_action"]),
        "",
        "## Why this distinction matters",
        "",
        str(report["why_the_distinction_matters"]),
        "",
    ]

    for family in ("input", "prediction", "concept"):
        group = report["signals"][family]
        status = "BREACHED" if group["breached"] else "within thresholds"
        lines += [
            f"## {family.capitalize()} drift — {status}",
            "",
            "| Signal | Statistic | Threshold | Breached | Interpretation |",
            "|---|---|---|---|---|",
        ]
        for measure in group["measures"]:
            lines.append(
                f"| `{measure['name']}` | {measure['statistic']:.4f} | "
                f"{measure['threshold']} | "
                f"{'**yes**' if measure['breached'] else 'no'} | "
                f"{measure['interpretation']} |"
            )
        lines.append("")

    return "\n".join(lines)


def dumps(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
