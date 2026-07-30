"""Single source of truth for values that must not be scattered through code.

Thresholds, seeds, gate margins and artifact schema versions all live here so
that changing a decision is a one-line edit with one place to review, and so
the M5 retrain gate reads the same numbers the M1 evaluation wrote.
"""

from __future__ import annotations

from typing import Final

# --- Document classes ------------------------------------------------------
# Order is significant and frozen: it defines the column order of every
# confusion matrix and per-class array in metrics.json and the baseline
# artifact. Appending is safe; reordering invalidates stored artifacts.
DOCUMENT_CLASSES: Final[tuple[str, ...]] = (
    "invoice",
    "medical_report",
    "id_document",
    "correspondence",
)

# --- Reproducibility -------------------------------------------------------
# Every random draw in generation, splitting and training derives from this.
# Passed explicitly rather than set globally, so a caller can vary it without
# mutating process state.
DEFAULT_SEED: Final[int] = 20260730

# --- Data generation -------------------------------------------------------
DEFAULT_DOCS_PER_CLASS: Final[int] = 400
# Held out of training entirely and asserted non-overlapping at evaluation
# time. Small enough to stay cheap, large enough that a per-class F1 moves by
# less than a rounding error when one document flips.
DEFAULT_GOLDEN_PER_CLASS: Final[int] = 60

# --- Model -----------------------------------------------------------------
# TF-IDF + linear is a deliberate choice: accuracy is an explicit non-goal, and
# a model that trains in seconds keeps the platform loop fast to exercise.
TFIDF_MAX_FEATURES: Final[int] = 20_000
TFIDF_NGRAM_RANGE: Final[tuple[int, int]] = (1, 2)
TFIDF_MIN_DF: Final[int] = 2

# --- Calibration -----------------------------------------------------------
# The intake Route state gates auto-approval on confidence, so a miscalibrated
# probability makes that gate meaningless regardless of accuracy. 10 equal-width
# bins is the conventional default for ECE and is what the reliability diagram
# in the evidence folder plots.
CALIBRATION_BINS: Final[int] = 10

# --- Routing (consumed by M3; defined here so M1 can measure against it) ---
# A document whose top-class probability is below this parks for human review.
AUTO_APPROVE_CONFIDENCE_THRESHOLD: Final[float] = 0.80

# --- Retrain gate (consumed by M5; defined here so one gate exists) --------
# A candidate must beat the champion by this much macro-F1 to be registered as
# a deployment candidate. Set above plausible eval noise on a 240-document
# golden set so the gate does not fire on a coin flip.
GATE_MIN_MACRO_F1_IMPROVEMENT: Final[float] = 0.02
# ...and must not drop any single class below this, so an overall gain that
# hides one collapsed class cannot pass.
GATE_MIN_PER_CLASS_F1: Final[float] = 0.60

# --- Artifact schema versions ---------------------------------------------
# These are contracts between milestones. Bump when the shape changes, and the
# consumer should refuse to read a version it does not know.
METRICS_SCHEMA_VERSION: Final[str] = "1.0.0"
BASELINE_SCHEMA_VERSION: Final[str] = "1.0.0"

# --- Filenames written into the artifact tarball / output paths ------------
MODEL_FILENAME: Final[str] = "model.joblib"
METRICS_FILENAME: Final[str] = "metrics.json"
BASELINE_FILENAME: Final[str] = "baseline_statistics.json"
LINEAGE_FILENAME: Final[str] = "lineage.json"
SNAPSHOT_MANIFEST_FILENAME: Final[str] = "snapshot.json"
