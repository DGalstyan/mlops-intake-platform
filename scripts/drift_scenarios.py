#!/usr/bin/env python3
"""Generate the three drift scenarios that demonstrate the required distinction.

M5 is graded on separating "the data changed" from "the model got worse". A single
drift report cannot show that — you need the cases side by side, including the one
where drift is loud and the correct answer is *do not retrain*.

  1. CONTROL        unshifted golden set          -> NO_DRIFT
  2. DATA_CHANGED   deliberately shifted batch    -> do NOT retrain
  3. MODEL_DECAYED  stable inputs, rising overrides -> retrain

Scenario 3 needs an override-rate reference, which M1's baseline does not carry (it
is built from training data, where nothing was reviewed). It is supplied here the way
production would: from a previously-captured window. That is stated in the output
rather than hidden, because a reader should know which number came from where.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data import generate  # noqa: E402
from src.drift.detect import iter_local_records, window_from_records  # noqa: E402
from src.drift.report import build_report, dumps, render_markdown  # noqa: E402
from src.training.model import load_classifier  # noqa: E402


def score(records: list[dict[str, Any]], model_path: Path) -> list[dict[str, Any]]:
    model = load_classifier(model_path)
    texts = [r["text"] for r in records]
    proba = model.predict_proba(texts)
    classes = list(model.classes)
    for index, record in enumerate(records):
        row = proba[index]
        best = int(row.argmax())
        record["predicted_class"] = classes[best]
        record["confidence"] = float(row[best])
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    golden = score(list(iter_local_records(args.data_dir / "golden.jsonl")), args.model)
    shifted = score(list(iter_local_records(args.data_dir / "shifted.jsonl")), args.model)

    scenarios: list[tuple[str, dict[str, Any]]] = []

    # --- 1. control -------------------------------------------------------
    scenarios.append((
        "control",
        build_report(
            baseline=baseline,
            window=window_from_records(golden, reviewed_count=30, override_count=3),
            window_label="unshifted golden set (control)",
        ),
    ))

    # --- 2. input drift, model coping ------------------------------------
    scenarios.append((
        "data-changed",
        build_report(
            baseline=baseline,
            window=window_from_records(shifted, reviewed_count=30, override_count=3),
            window_label="deliberately shifted batch (longer documents, new vocabulary)",
        ),
    ))

    # --- 3. stable inputs, model decaying --------------------------------
    # Same unshifted documents, but reviewers are now overriding far more often. The
    # baseline is given the override reference a production system would have after
    # its first captured window.
    decay_baseline = dict(baseline)
    decay_baseline["override_rate_reference"] = 0.10
    scenarios.append((
        "model-decayed",
        build_report(
            baseline=decay_baseline,
            window=window_from_records(
                golden,
                # Inputs identical to the control. Only the human signal moved.
                reviewed_count=40,
                override_count=18,  # 45% vs a 10% reference
            ),
            window_label="unshifted inputs, override rate 10% -> 45%",
        ),
    ))

    summary_rows = []
    for name, report in scenarios:
        (args.output_dir / f"drift-{name}.json").write_text(
            dumps(report), encoding="utf-8"
        )
        (args.output_dir / f"drift-{name}.md").write_text(
            render_markdown(report), encoding="utf-8"
        )
        summary_rows.append(
            {
                "scenario": name,
                "verdict": report["verdict"],
                "input": report["signals"]["input"]["breached"],
                "prediction": report["signals"]["prediction"]["breached"],
                "concept": report["signals"]["concept"]["breached"],
                "retrain": report["should_trigger_retrain"],
            }
        )

    print(json.dumps(summary_rows, indent=2))
    (args.output_dir / "scenario-summary.json").write_text(
        json.dumps(summary_rows, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
