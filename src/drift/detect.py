"""Drift detection entrypoint — scheduled Lambda handler and CLI.

Reads a production window (from endpoint data-capture in AWS, or from a local JSONL
file when run offline), compares it to the M1 baseline, writes the report to S3, and
publishes to SNS on a breach.

Runs as a Lambda on a schedule rather than as a SageMaker Processing job. The
comparison is a few hundred kilobytes of histogram arithmetic over a bounded window —
a Processing job would cost more in container startup than the computation, and the
job's only advantage (arbitrary scale) is not needed until the window exceeds what
fits in Lambda memory. That threshold is stated in the decision log so it is a
decision rather than an assumption.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.drift.report import (  # noqa: E402
    ProductionWindow,
    build_report,
    dumps,
    render_markdown,
)

logger = logging.getLogger("intake.drift")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def log_event(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str))


def parse_capture_record(line: str) -> dict[str, Any] | None:
    """Parse one SageMaker data-capture record into (text, class, confidence).

    Capture records wrap the endpoint's request and response as strings inside a
    JSON envelope, so this has to unwrap two layers. Returns None for a record it
    cannot read rather than raising: a scheduled job that dies on one malformed line
    reports nothing at all, which is worse than reporting on the rest and saying how
    many it skipped.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None

    capture = record.get("captureData") or {}
    request = capture.get("endpointInput") or {}
    response = capture.get("endpointOutput") or {}

    try:
        request_body = json.loads(request.get("data", "{}"))
        response_body = json.loads(response.get("data", "{}"))
    except (json.JSONDecodeError, TypeError):
        return None

    predictions = response_body.get("predictions") or []
    if not predictions:
        return None
    prediction = predictions[0]

    text = request_body.get("text")
    if not isinstance(text, str):
        texts = request_body.get("texts")
        text = texts[0] if isinstance(texts, list) and texts else None
    if not isinstance(text, str):
        return None

    predicted = prediction.get("predicted_class")
    confidence = prediction.get("confidence")
    if not isinstance(predicted, str) or not isinstance(confidence, (int, float)):
        return None

    return {
        "text": text,
        "predicted_class": predicted,
        "confidence": float(confidence),
    }


def iter_local_records(path: Path) -> Iterator[dict[str, Any]]:
    """Read a local JSONL window. Supports both capture-shaped and plain records."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            parsed = parse_capture_record(line)
            if parsed:
                yield parsed
                continue
            # A plain {"text", "label"} record, as the generator emits.
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and isinstance(raw.get("text"), str):
                yield raw


def window_from_records(
    records: Sequence[dict[str, Any]],
    *,
    reviewed_count: int = 0,
    override_count: int = 0,
    schema_failure_count: int = 0,
) -> ProductionWindow:
    return ProductionWindow(
        texts=[r["text"] for r in records],
        predicted_classes=[r.get("predicted_class", "") for r in records],
        confidences=[float(r.get("confidence", 0.0)) for r in records],
        reviewed_count=reviewed_count,
        override_count=override_count,
        schema_failure_count=schema_failure_count,
    )


def run_local(
    *,
    baseline_path: Path,
    window_path: Path,
    output_dir: Path,
    window_label: str,
    classify_with_model: Path | None = None,
    reviewed_count: int = 0,
    override_count: int = 0,
) -> dict[str, Any]:
    """Offline drift run. This is what produces the M5 evidence."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    records = list(iter_local_records(window_path))
    if not records:
        raise ValueError(f"no usable records in {window_path}")

    # A window read from raw documents has no predictions attached. Scoring it with
    # the champion model is what makes prediction drift and confidence decay
    # computable offline — in production those come from data capture, where the
    # endpoint already recorded them.
    if classify_with_model is not None:
        from src.training.model import load_classifier

        model = load_classifier(classify_with_model)
        texts = [r["text"] for r in records]
        proba = model.predict_proba(texts)
        classes = list(model.classes)
        for index, record in enumerate(records):
            row = proba[index]
            best = int(row.argmax())
            record["predicted_class"] = classes[best]
            record["confidence"] = float(row[best])

    window = window_from_records(
        records,
        reviewed_count=reviewed_count,
        override_count=override_count,
    )
    report = build_report(
        baseline=baseline, window=window, window_label=window_label
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "drift-report.json").write_text(dumps(report), encoding="utf-8")
    (output_dir / "drift-report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    return report


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Scheduled entrypoint. Reads capture from S3, writes the report back, notifies."""
    import boto3

    baseline_uri = os.environ["BASELINE_S3_URI"]
    capture_prefix = os.environ["DATA_CAPTURE_S3_PREFIX"]
    report_bucket = os.environ["REPORT_BUCKET"]
    topic_arn = os.environ.get("ALARM_TOPIC_ARN", "")

    s3 = boto3.client("s3")

    def split_uri(uri: str) -> tuple[str, str]:
        without_scheme = uri.replace("s3://", "", 1)
        bucket, _, key = without_scheme.partition("/")
        return bucket, key

    baseline_bucket, baseline_key = split_uri(baseline_uri)
    baseline = json.loads(
        s3.get_object(Bucket=baseline_bucket, Key=baseline_key)["Body"]
        .read()
        .decode("utf-8")
    )

    capture_bucket, capture_key_prefix = split_uri(capture_prefix)
    paginator = s3.get_paginator("list_objects_v2")

    records: list[dict[str, Any]] = []
    skipped = 0
    for page in paginator.paginate(Bucket=capture_bucket, Prefix=capture_key_prefix):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=capture_bucket, Key=obj["Key"])["Body"].read()
            text = (
                gzip.decompress(body).decode("utf-8")
                if obj["Key"].endswith(".gz")
                else body.decode("utf-8")
            )
            for line in text.splitlines():
                if not line.strip():
                    continue
                parsed = parse_capture_record(line)
                if parsed:
                    records.append(parsed)
                else:
                    skipped += 1

    if not records:
        # Not an error. An idle pipeline produces no capture, and a scheduled job that
        # fails on quiet periods gets muted.
        log_event("drift_skipped", reason="no capture records in window")
        return {"status": "NO_DATA", "skipped": skipped}

    window = window_from_records(records)
    report = build_report(
        baseline=baseline,
        window=window,
        window_label=f"{capture_prefix} ({len(records)} records)",
    )
    report["window"]["unparseable_records_skipped"] = skipped

    stamp = report["generated_at"].replace(":", "-")
    for suffix, body in (
        ("json", dumps(report)),
        ("md", render_markdown(report)),
    ):
        s3.put_object(
            Bucket=report_bucket,
            Key=f"drift-reports/{stamp}/drift-report.{suffix}",
            Body=body.encode("utf-8"),
            ContentType="application/json" if suffix == "json" else "text/markdown",
        )

    log_event(
        "drift_report_written",
        verdict=report["verdict"],
        documents=window.size,
        skipped=skipped,
        triggers_retrain=report["should_trigger_retrain"],
    )

    # Notify on ANY verdict other than NO_DRIFT — including DATA_CHANGED, which does
    # not trigger a retrain but is exactly the thing a human should look at before it
    # becomes decay.
    if topic_arn and report["verdict"] != "NO_DRIFT":
        boto3.client("sns").publish(
            TopicArn=topic_arn,
            Subject=f"Drift detected: {report['verdict']}"[:100],
            Message=render_markdown(report)[:262000],
        )

    return {
        "status": "OK",
        "verdict": report["verdict"],
        "should_trigger_retrain": report["should_trigger_retrain"],
        "report_prefix": f"s3://{report_bucket}/drift-reports/{stamp}/",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--window", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--label", default="local-window")
    parser.add_argument(
        "--classify-with",
        type=Path,
        default=None,
        help="Score the window with this model. Needed when the window is raw documents rather than data-capture records.",
    )
    parser.add_argument("--reviewed-count", type=int, default=0)
    parser.add_argument("--override-count", type=int, default=0)
    args = parser.parse_args(argv)

    report = run_local(
        baseline_path=args.baseline,
        window_path=args.window,
        output_dir=args.output_dir,
        window_label=args.label,
        classify_with_model=args.classify_with,
        reviewed_count=args.reviewed_count,
        override_count=args.override_count,
    )
    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "should_trigger_retrain": report["should_trigger_retrain"],
                "input_breached": report["signals"]["input"]["breached"],
                "prediction_breached": report["signals"]["prediction"]["breached"],
                "concept_breached": report["signals"]["concept"]["breached"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
