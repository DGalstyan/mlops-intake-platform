"""SageMaker Processing entrypoint — evaluate a candidate on the frozen golden set.

These are the numbers that matter: the release decision and the M5 retrain gate
both read this file, and nothing else in the system produces a held-out score.

The non-overlap assertion is the important part of this script. Training on the
golden set inflates every downstream number *and* the baseline artifact, and it
does so in the direction that looks like success — so no alarm, dashboard or gate
would catch it. It is checked here, at evaluation time, because that is the last
point where both splits are in scope, and it fails the job loudly rather than
warning.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    CALIBRATION_BINS,
    DOCUMENT_CLASSES,
    GATE_MIN_MACRO_F1_IMPROVEMENT,
    GATE_MIN_PER_CLASS_F1,
    METRICS_FILENAME,
    METRICS_SCHEMA_VERSION,
    MODEL_FILENAME,
    SNAPSHOT_MANIFEST_FILENAME,
)
from src.data.generate import Document, assert_disjoint, read_jsonl  # noqa: E402
from src.training import lineage as lineage_module  # noqa: E402
from src.training import metrics as metrics_module  # noqa: E402
from src.training.model import dumps_canonical, load_classifier  # noqa: E402


def _load_split(path: Path, what: str) -> list[Document]:
    if not path.is_file():
        raise FileNotFoundError(f"no {what} data at {path}")
    documents = read_jsonl(path)
    if not documents:
        raise ValueError(f"{path} contained no documents")
    return documents


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--champion-metrics",
        type=Path,
        default=None,
        help=(
            "Existing champion's golden-set metrics.json. When given, the gate "
            "decision is computed and written alongside the candidate metrics."
        ),
    )
    return parser.parse_args(argv)


def evaluate_gate(
    candidate: dict[str, object],
    champion: dict[str, object] | None,
) -> dict[str, object]:
    """Decide whether a candidate is allowed to become a deployment candidate.

    Two independent conditions, both of which must hold:

    1. macro-F1 must beat the champion by at least
       GATE_MIN_MACRO_F1_IMPROVEMENT. A margin rather than ">" because on a
       240-document golden set the difference between two runs is often noise,
       and a gate that fires on noise trains people to ignore it.
    2. no single class may fall below GATE_MIN_PER_CLASS_F1. An overall gain that
       hides one collapsed class is the failure mode macro-averaging alone does
       not catch — a model can improve on aggregate while becoming useless for
       `id_document`, and the humans reviewing that queue would feel it long
       before the headline metric did.

    With no champion (the first ever version) condition 1 cannot be evaluated, so
    only the per-class floor applies. That is stated in the output rather than
    silently passing.
    """
    candidate_macro = float(candidate["macro_f1"])  # type: ignore[arg-type]
    per_class = candidate["per_class"]
    assert isinstance(per_class, list)

    failing_classes = [
        {"label": entry["label"], "f1": entry["f1"]}
        for entry in per_class
        if float(entry["f1"]) < GATE_MIN_PER_CLASS_F1
    ]
    floor_ok = not failing_classes

    if champion is None:
        return {
            "passed": floor_ok,
            "reason": (
                "no champion to compare against; per-class floor only"
                if floor_ok
                else "per-class floor not met"
            ),
            "is_first_version": True,
            "candidate_macro_f1": candidate_macro,
            "champion_macro_f1": None,
            "required_improvement": GATE_MIN_MACRO_F1_IMPROVEMENT,
            "actual_improvement": None,
            "per_class_floor": GATE_MIN_PER_CLASS_F1,
            "failing_classes": failing_classes,
        }

    champion_macro = float(champion["macro_f1"])  # type: ignore[arg-type]
    improvement = candidate_macro - champion_macro
    improvement_ok = improvement >= GATE_MIN_MACRO_F1_IMPROVEMENT

    reasons: list[str] = []
    if not improvement_ok:
        reasons.append(
            f"macro-F1 improvement {improvement:+.4f} is below the required "
            f"{GATE_MIN_MACRO_F1_IMPROVEMENT:+.4f}"
        )
    if not floor_ok:
        reasons.append(
            "classes below the per-class floor: "
            + ", ".join(f"{c['label']}={float(c['f1']):.3f}" for c in failing_classes)
        )

    return {
        "passed": improvement_ok and floor_ok,
        "reason": "; ".join(reasons) if reasons else "candidate beats champion",
        "is_first_version": False,
        "candidate_macro_f1": candidate_macro,
        "champion_macro_f1": champion_macro,
        "required_improvement": GATE_MIN_MACRO_F1_IMPROVEMENT,
        "actual_improvement": improvement,
        "per_class_floor": GATE_MIN_PER_CLASS_F1,
        "failing_classes": failing_classes,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    golden = _load_split(args.data_dir / "golden.jsonl", "golden")

    # The leakage check. Loaded only for this assertion — the model is never
    # scored against the training split here.
    train_path = args.data_dir / "train.jsonl"
    if train_path.is_file():
        assert_disjoint(read_jsonl(train_path), golden)
    else:
        print(
            f"WARNING: {train_path} not present, so train/golden non-overlap "
            "could not be verified. Any metric produced here is unproven.",
            file=sys.stderr,
        )

    classifier = load_classifier(args.model_dir / MODEL_FILENAME)

    texts = [d.text for d in golden]
    truths = [d.label for d in golden]
    predictions = classifier.predict(texts)
    proba = classifier.predict_proba(texts)

    # The classifier's own column order, not the config order, is what proba's
    # columns mean. Reorder to the canonical order so the confusion matrix and
    # every stored per-class array line up with config.DOCUMENT_CLASSES.
    model_classes = list(classifier.classes)
    canonical = list(DOCUMENT_CLASSES)
    if model_classes != canonical:
        missing = set(canonical) - set(model_classes)
        if missing:
            raise ValueError(
                f"model does not predict these classes at all: {sorted(missing)}"
            )
        order = [model_classes.index(label) for label in canonical]
        proba = proba[:, order]

    results = metrics_module.evaluate(
        truths, predictions, proba, canonical, n_bins=CALIBRATION_BINS
    )
    results.update(
        {
            "schema_version": METRICS_SCHEMA_VERSION,
            "split": "golden",
            "is_held_out": True,
            "non_overlap_verified": train_path.is_file(),
            "data_snapshot_id": lineage_module.read_snapshot_id(
                args.data_dir, manifest_filename=SNAPSHOT_MANIFEST_FILENAME
            ),
            "git_sha": lineage_module.resolve_git_sha(),
        }
    )

    champion: dict[str, object] | None = None
    if args.champion_metrics is not None and args.champion_metrics.is_file():
        # Loaded into a non-optional local first: json.loads returns Any, so
        # assigning straight into the Optional above leaves mypy unable to
        # narrow it and the .get() calls below unverifiable.
        loaded: dict[str, object] = json.loads(
            args.champion_metrics.read_text(encoding="utf-8")
        )
        if loaded.get("split") != "golden":
            raise ValueError(
                "champion metrics were not computed on the golden split "
                f"(split={loaded.get('split')!r}); comparing them to a "
                "held-out score would be meaningless"
            )
        champion = loaded

    results["gate"] = evaluate_gate(results, champion)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / METRICS_FILENAME).write_text(
        dumps_canonical(results), encoding="utf-8"
    )

    gate = results["gate"]
    assert isinstance(gate, dict)
    print(
        json.dumps(
            {
                "macro_f1": round(float(results["macro_f1"]), 4),
                "accuracy": round(float(results["accuracy"]), 4),
                "expected_calibration_error": round(
                    float(results["expected_calibration_error"]), 4
                ),
                "n_golden": results["n_samples"],
                "non_overlap_verified": results["non_overlap_verified"],
                "gate_passed": gate["passed"],
                "gate_reason": gate["reason"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
