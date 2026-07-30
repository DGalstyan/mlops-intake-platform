#!/usr/bin/env python3
"""Post-deploy smoke test. Exits non-zero if the endpoint's contract is broken.

This is a **release gate**, not a monitoring check. It runs immediately after a
deployment and its exit code decides whether the release is accepted, so what it
asserts is chosen to catch the failures that a healthy-looking endpoint can still
have:

- the response still has the exact keys M3, M4 and M5 read (a rename is invisible
  to CloudWatch: the endpoint returns 200 and every metric looks perfect),
- confidence is a usable probability, not a degenerate 1.0 for everything,
- malformed input is rejected with 4xx rather than 5xx — because 5xx drives the
  rollback alarm, so a contract regression here would make bad requests roll back
  good deployments,
- every configured class appears in the probability distribution, since M5 indexes
  by class name.

Deliberately does **not** assert accuracy. Model quality is measured on the golden
set at M1, against labels this script does not have; asserting predictions here
would make the release gate fail for a legitimately retrained model.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Sequence

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Imported from config, never duplicated. An earlier revision carried a hardcoded
# fallback copy of these constants for the case where the script is dropped into a
# CI image without the package. That was actively harmful: one of the assertions
# below checks that the *endpoint's* reported threshold matches config, to catch a
# deployed image built against stale config. A duplicated constant here could
# drift from config.py and make that check compare two wrong numbers and pass.
from src.config import AUTO_APPROVE_CONFIDENCE_THRESHOLD, DOCUMENT_CLASSES  # noqa: E402

REQUIRED_PREDICTION_KEYS = {
    "predicted_class",
    "confidence",
    "class_probabilities",
    "auto_approve_eligible",
    "confidence_threshold",
}
REQUIRED_ENVELOPE_KEYS = {"schema_version", "predictions", "model_version"}

# Representative documents, one per class. Vocabulary-only, no ground-truth
# assertion — see the module docstring.
PROBE_DOCUMENTS: dict[str, str] = {
    "invoice": "document 100001 invoice amount due payable vat subtotal remittance vendor billing terms net",
    "medical_report": "document 100002 patient specimen clinician findings impression laboratory reference range abnormal",
    "id_document": "document 100003 passport surname given names nationality birth expiry issuing authority holder",
    "correspondence": "document 100004 letter sincerely enquiry response acknowledge notice follow meeting confirm",
}


class SmokeTestFailure(AssertionError):
    """A contract violation that must block the release."""


def _check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def validate_response(payload: dict[str, Any], *, label: str) -> list[str]:
    """Validate one response against the cross-milestone contract."""
    failures: list[str] = []

    missing_envelope = REQUIRED_ENVELOPE_KEYS - set(payload)
    _check(
        not missing_envelope,
        f"[{label}] response is missing top-level keys: {sorted(missing_envelope)}",
        failures,
    )

    predictions = payload.get("predictions")
    if not isinstance(predictions, list) or not predictions:
        failures.append(f"[{label}] 'predictions' is missing or empty")
        return failures

    prediction = predictions[0]
    missing = REQUIRED_PREDICTION_KEYS - set(prediction)
    extra = set(prediction) - REQUIRED_PREDICTION_KEYS
    _check(
        not missing,
        f"[{label}] prediction is missing contract keys: {sorted(missing)}",
        failures,
    )
    _check(
        not extra,
        f"[{label}] prediction has unexpected keys {sorted(extra)} — additions are "
        "safe for consumers but must be a deliberate schema_version change",
        failures,
    )
    if missing:
        return failures

    predicted = prediction["predicted_class"]
    _check(
        predicted in DOCUMENT_CLASSES,
        f"[{label}] predicted_class {predicted!r} is not a configured class",
        failures,
    )

    confidence = prediction["confidence"]
    _check(
        isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0,
        f"[{label}] confidence {confidence!r} is not a probability in [0, 1]",
        failures,
    )

    probabilities = prediction["class_probabilities"]
    if isinstance(probabilities, dict):
        missing_classes = set(DOCUMENT_CLASSES) - set(probabilities)
        _check(
            not missing_classes,
            f"[{label}] class_probabilities omits {sorted(missing_classes)}; M5 "
            "indexes prediction drift by class name and would KeyError",
            failures,
        )
        total = sum(float(v) for v in probabilities.values())
        _check(
            abs(total - 1.0) < 1e-6,
            f"[{label}] class_probabilities sum to {total:.6f}, not 1.0",
            failures,
        )
        _check(
            abs(float(probabilities.get(predicted, -1)) - float(confidence)) < 1e-9,
            f"[{label}] confidence does not equal the probability of the "
            "predicted class — the two have drifted apart",
            failures,
        )
    else:
        failures.append(f"[{label}] class_probabilities is not an object")

    _check(
        prediction["auto_approve_eligible"]
        == (float(confidence) >= float(prediction["confidence_threshold"])),
        f"[{label}] auto_approve_eligible disagrees with confidence vs threshold",
        failures,
    )
    _check(
        float(prediction["confidence_threshold"])
        == float(AUTO_APPROVE_CONFIDENCE_THRESHOLD),
        f"[{label}] endpoint reports threshold "
        f"{prediction['confidence_threshold']} but config says "
        f"{AUTO_APPROVE_CONFIDENCE_THRESHOLD} — the deployed image is stale",
        failures,
    )
    return failures


def run(
    endpoint_name: str,
    *,
    region: str | None,
    require_confidence_spread: bool,
) -> int:
    import boto3
    from botocore.exceptions import ClientError

    runtime = boto3.client("sagemaker-runtime", region_name=region)
    failures: list[str] = []
    confidences: list[float] = []
    latencies_ms: list[float] = []

    for label, text in PROBE_DOCUMENTS.items():
        started = time.perf_counter()
        try:
            response = runtime.invoke_endpoint(
                EndpointName=endpoint_name,
                ContentType="application/json",
                Accept="application/json",
                Body=json.dumps({"text": text}),
            )
        except ClientError as error:
            failures.append(f"[{label}] invoke_endpoint failed: {error}")
            continue
        latencies_ms.append((time.perf_counter() - started) * 1000.0)

        raw = response["Body"].read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            failures.append(f"[{label}] response is not valid JSON: {error}")
            continue

        failures.extend(validate_response(payload, label=label))
        prediction = (payload.get("predictions") or [{}])[0]
        if isinstance(prediction.get("confidence"), (int, float)):
            confidences.append(float(prediction["confidence"]))

    # A malformed request must be rejected as a client error. If this regressed to
    # 5xx, bad input would trip the rollback alarm and undo healthy deployments.
    try:
        runtime.invoke_endpoint(
            EndpointName=endpoint_name,
            ContentType="application/json",
            Body=b"{not json",
        )
        failures.append(
            "[malformed] endpoint accepted invalid JSON instead of rejecting it"
        )
    except ClientError as error:
        status = (
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
        )
        if status >= 500:
            failures.append(
                f"[malformed] invalid JSON returned {status}. It must be 4xx: a 5xx "
                "here feeds the rollback alarm, so malformed client requests would "
                "roll back healthy deployments."
            )

    # Degenerate confidence means either a leaked label in training or a broken
    # calibrator. Optional because a genuinely easy model could legitimately be
    # confident on four hand-written probes.
    if require_confidence_spread and confidences:
        if all(c > 0.999 for c in confidences):
            failures.append(
                f"[calibration] every probe returned confidence >0.999 "
                f"({confidences}). The confidence gate cannot function; suspect a "
                "leaked label in training data or a bypassed calibrator."
            )

    summary = {
        "endpoint": endpoint_name,
        "probes": len(PROBE_DOCUMENTS),
        "confidences": [round(c, 4) for c in confidences],
        "latency_ms_mean": round(sum(latencies_ms) / len(latencies_ms), 1)
        if latencies_ms
        else None,
        "failures": failures,
        "passed": not failures,
    }
    print(json.dumps(summary, indent=2))

    if failures:
        print(
            f"\nSMOKE TEST FAILED with {len(failures)} contract violation(s). "
            "Blocking the release.",
            file=sys.stderr,
        )
        return 1
    print("\nSMOKE TEST PASSED", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint-name", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--require-confidence-spread",
        action="store_true",
        help="Fail if every probe returns confidence >0.999 (degenerate calibration).",
    )
    args = parser.parse_args(argv)
    return run(
        args.endpoint_name,
        region=args.region,
        require_confidence_spread=args.require_confidence_spread,
    )


if __name__ == "__main__":
    sys.exit(main())
