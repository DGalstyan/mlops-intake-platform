"""SageMaker training entrypoint (script mode).

Reads the SageMaker channel conventions from the environment so the same script
runs unchanged locally and in a training job:

  SM_CHANNEL_TRAIN   input data directory
  SM_MODEL_DIR       where model.tar.gz contents are collected from
  SM_OUTPUT_DATA_DIR where side-artifacts (metrics, baseline, lineage) go

Outputs, all written deterministically for a fixed seed:

  model.joblib               the serialised classifier
  metrics.json               training-set metrics (NOT the gate's numbers)
  baseline_statistics.json   reference distributions for M5 drift detection
  lineage.json               snapshot id, git SHA, image digest, resolved deps

A note on what `metrics.json` from *this* script means: these are training-set
numbers, and they are recorded for debugging and for the "did the fit actually
converge" question only. The numbers that gate a release come from evaluate.py
running against the frozen golden set, which this script never sees. Keeping both
files but labelling their `split` field is deliberate — reporting a training-set
macro-F1 as if it were a held-out score is the easiest way to accidentally lie in
a model card.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    BASELINE_FILENAME,
    CALIBRATION_BINS,
    DEFAULT_SEED,
    DOCUMENT_CLASSES,
    LINEAGE_FILENAME,
    METRICS_FILENAME,
    METRICS_SCHEMA_VERSION,
    MODEL_FILENAME,
    SNAPSHOT_MANIFEST_FILENAME,
)
from src.data.generate import read_jsonl  # noqa: E402
from src.training import baseline as baseline_module  # noqa: E402
from src.training import lineage as lineage_module  # noqa: E402
from src.training import metrics as metrics_module  # noqa: E402
from src.training.model import (  # noqa: E402
    build_classifier,
    describe_environment,
    dumps_canonical,
)


def seed_everything(seed: int) -> None:
    """Pin every RNG the training path can reach.

    scikit-learn estimators take `random_state` explicitly (see model.py), so
    this covers the library-level draws that do not — and makes the run
    reproducible even if a future implementation adds one that reads global
    state.
    """
    random.seed(seed)
    np.random.seed(seed % (2**32))
    os.environ["PYTHONHASHSEED"] = str(seed)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--train-dir",
        type=Path,
        default=Path(os.environ.get("SM_CHANNEL_TRAIN", "data/processed")),
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.environ.get("SM_MODEL_DIR", "artifacts/model")),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("SM_OUTPUT_DATA_DIR", "artifacts/output")),
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--no-calibration",
        action="store_true",
        help=(
            "Fit without probability calibration. Used to produce a second, "
            "deliberately worse-calibrated registry version so the two versions "
            "differ in a way that matters to the confidence gate, not just by "
            "random noise."
        ),
    )
    parser.add_argument(
        "--tfidf-max-features",
        type=int,
        default=None,
        help="Override the feature cap. Another axis for producing a distinguishable second version.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    seed_everything(args.seed)

    train_path = args.train_dir / "train.jsonl"
    if not train_path.is_file():
        raise FileNotFoundError(
            f"no training data at {train_path}. Run "
            "`python -m src.data.generate --output-dir <dir>` first."
        )

    documents = read_jsonl(train_path)
    if not documents:
        raise ValueError(f"{train_path} contained no documents")
    texts = [d.text for d in documents]
    labels = [d.label for d in documents]

    classifier_kwargs: dict[str, object] = {
        "seed": args.seed,
        "calibrate": not args.no_calibration,
    }
    if args.tfidf_max_features is not None:
        classifier_kwargs["max_features"] = args.tfidf_max_features

    classifier = build_classifier(**classifier_kwargs)
    classifier.fit(texts, labels)

    # --- model artifact ---
    args.model_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.model_dir / MODEL_FILENAME
    classifier.save(model_path)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # --- lineage ---
    hyperparameters = (
        classifier.hyperparameters()
        if hasattr(classifier, "hyperparameters")
        else {}
    )
    lineage = lineage_module.collect(
        data_snapshot_id=lineage_module.read_snapshot_id(
            args.train_dir, manifest_filename=SNAPSHOT_MANIFEST_FILENAME
        ),
        environment=describe_environment(),
        hyperparameters=hyperparameters,
    )
    (args.output_dir / LINEAGE_FILENAME).write_text(
        lineage.to_json(), encoding="utf-8"
    )
    if not lineage.is_complete():
        print(
            f"WARNING: incomplete lineage, missing {lineage.missing_fields()}. "
            "A registry version with unknown provenance cannot be reproduced.",
            file=sys.stderr,
        )

    # --- training-set metrics (explicitly labelled as such) ---
    proba = classifier.predict_proba(texts)
    predictions = classifier.predict(texts)
    training_metrics = metrics_module.evaluate(
        labels, predictions, proba, list(DOCUMENT_CLASSES), n_bins=CALIBRATION_BINS
    )
    training_metrics.update(
        {
            "schema_version": METRICS_SCHEMA_VERSION,
            # The single most important field in this file. Anything reading a
            # macro_f1 should check this before comparing it to another one.
            "split": "train",
            "is_held_out": False,
            "note": (
                "Training-set metrics, for convergence debugging only. Release "
                "and retrain-gate decisions use evaluate.py's golden-set "
                "metrics, which are held out."
            ),
            "data_snapshot_id": lineage.data_snapshot_id,
            "git_sha": lineage.git_sha,
        }
    )
    (args.output_dir / METRICS_FILENAME).write_text(
        dumps_canonical(training_metrics), encoding="utf-8"
    )

    # --- baseline statistics (the M5 contract) ---
    # Built from the training split, because that is what "normal" means for
    # this model — see the module docstring in baseline.py.
    feature_matrix = None
    feature_names = None
    pipeline = getattr(classifier, "_pipeline", None)
    if pipeline is not None and "tfidf" in getattr(pipeline, "named_steps", {}):
        vectorizer = pipeline.named_steps["tfidf"]
        feature_matrix = vectorizer.transform(texts)
        feature_names = list(vectorizer.get_feature_names_out())

    baseline_artifact = baseline_module.build_baseline(
        texts=texts,
        predictions=predictions,
        confidences=metrics_module.top_class_confidence(proba).tolist(),
        labels=list(DOCUMENT_CLASSES),
        snapshot_id=lineage.data_snapshot_id,
        git_sha=lineage.git_sha,
        feature_matrix=feature_matrix,
        feature_names=feature_names,
    )
    (args.output_dir / BASELINE_FILENAME).write_text(
        dumps_canonical(baseline_artifact), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "model": str(model_path),
                "train_macro_f1": round(training_metrics["macro_f1"], 4),
                "train_ece": round(
                    training_metrics["expected_calibration_error"], 4
                ),
                "n_train": len(documents),
                "data_snapshot_id": lineage.data_snapshot_id,
                "git_sha": lineage.git_sha,
                "calibrated": not args.no_calibration,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
