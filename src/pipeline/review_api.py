"""The reviewer-facing API: list pending reviews, submit a correction.

**Why this is a Lambda** (third and last): it is an HTTP API. There is nothing to
express in ASL — this code runs *outside* the state machine and resumes it by
calling `SendTaskSuccess` with the stored task token.

The token handling is the part worth care. A task token is a capability: whoever
holds it can resume that execution with arbitrary output. So the API never accepts a
token from the caller — it looks the token up from the review table by
correlation_id. A reviewer submits "correlation X should be class Y"; they never see
or supply a token. Accepting a caller-supplied token would let anyone who guessed
one inject a correction into any document.

Payload validation is strict for the same reason the inference handler's is: a
correction is written back as labelled training data and will be used to retrain the
model. A malformed correction that is accepted becomes a poisoned training label,
and nothing downstream would flag it.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Final, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import DOCUMENT_CLASSES  # noqa: E402

logger = logging.getLogger("intake.review_api")
logging.basicConfig(level=logging.INFO, format="%(message)s")

MAX_REVIEWER_ID_LENGTH: Final[int] = 128
MAX_NOTE_LENGTH: Final[int] = 2000


class ReviewApiError(Exception):
    """A client error. Carries the HTTP status the caller should see."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class StepFunctionsClient(Protocol):
    def send_task_success(self, *, taskToken: str, output: str) -> dict[str, Any]: ...
    def send_task_failure(
        self, *, taskToken: str, error: str, cause: str
    ) -> dict[str, Any]: ...


class ReviewStore(Protocol):
    """The review queue, abstracted so the handler is testable without DynamoDB."""

    def get_pending(self, correlation_id: str) -> dict[str, Any] | None: ...
    def mark_completed(self, correlation_id: str, reviewer_id: str) -> None: ...


def validate_correction(payload: Any) -> dict[str, Any]:
    """Validate a submitted correction.

    Strict, because the output becomes a training label. Every rejection here is a
    label that would otherwise have quietly poisoned the next retrain.
    """
    if not isinstance(payload, dict):
        raise ReviewApiError(400, "body must be a JSON object")

    correlation_id = payload.get("correlation_id")
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        raise ReviewApiError(400, "correlation_id is required")

    reviewer_id = payload.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        # Required, not defaulted to "anonymous": the corrections table is labelled
        # training data, and a label with no attributable author cannot be audited
        # when the model trained on it starts behaving oddly.
        raise ReviewApiError(400, "reviewer_id is required")
    if len(reviewer_id) > MAX_REVIEWER_ID_LENGTH:
        raise ReviewApiError(400, "reviewer_id is too long")

    corrected_class = payload.get("corrected_class")
    if corrected_class not in DOCUMENT_CLASSES:
        raise ReviewApiError(
            400,
            f"corrected_class must be one of {list(DOCUMENT_CLASSES)}, "
            f"got {corrected_class!r}",
        )

    corrected_fields = payload.get("corrected_fields", {})
    if not isinstance(corrected_fields, dict):
        raise ReviewApiError(400, "corrected_fields must be an object")

    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH:
        raise ReviewApiError(400, "note must be a string under 2000 characters")

    return {
        "correlation_id": correlation_id.strip(),
        "reviewer_id": reviewer_id.strip(),
        "corrected_class": corrected_class,
        "corrected_fields": corrected_fields,
        "note": note,
    }


def build_task_output(
    correction: dict[str, Any], pending: dict[str, Any]
) -> dict[str, Any]:
    """Build the payload sent back into the waiting execution.

    `prediction_was_correct` is computed here rather than submitted by the reviewer.
    A reviewer confirming the class is not asserting anything about the model; the
    comparison is the system's own observation. Letting the client supply it would
    make the override rate — M5's concept-drift proxy — a self-reported number.
    """
    original_class = pending.get("predicted_class", "")
    return {
        "reviewer_id": correction["reviewer_id"],
        "corrected_class": correction["corrected_class"],
        "corrected_fields": correction["corrected_fields"],
        "prediction_was_correct": correction["corrected_class"] == original_class,
        # `.get` rather than indexing: `note` is optional and defaulted by
        # validate_correction, but this function should not 500 a reviewer's
        # submission if it is ever called with a payload that skipped that default.
        "note": correction.get("note", ""),
    }


def submit_correction(
    payload: Any,
    *,
    store: ReviewStore,
    stepfunctions: StepFunctionsClient,
) -> dict[str, Any]:
    """Validate a correction, resume the execution, and close the review task."""
    correction = validate_correction(payload)
    correlation_id = correction["correlation_id"]

    pending = store.get_pending(correlation_id)
    if pending is None:
        # Deliberately does not distinguish "never existed" from "already reviewed":
        # both mean there is nothing to act on, and the caller cannot fix either.
        raise ReviewApiError(
            404, f"no pending review for correlation_id {correlation_id!r}"
        )

    task_token = pending.get("task_token")
    if not task_token:
        raise ReviewApiError(
            409,
            f"review {correlation_id!r} has no task token; it may have timed out. "
            "The document will appear in the dead-letter queue.",
        )

    output = build_task_output(correction, pending)

    try:
        stepfunctions.send_task_success(
            taskToken=str(task_token), output=json.dumps(output)
        )
    except Exception as error:  # noqa: BLE001 - botocore error types are dynamic
        name = type(error).__name__
        if "TaskTimedOut" in name or "TaskTimedOut" in str(error):
            # The execution gave up waiting. The document has already dead-lettered,
            # so the reviewer's work cannot be applied — say so plainly rather than
            # reporting success.
            raise ReviewApiError(
                409,
                f"review {correlation_id!r} timed out before this correction was "
                "submitted; the document is in the dead-letter queue and must be "
                "replayed.",
            ) from error
        if "TaskDoesNotExist" in name or "InvalidToken" in name:
            raise ReviewApiError(
                409, f"review {correlation_id!r} has already been completed"
            ) from error
        raise

    # Marked completed only AFTER the execution has been resumed. The other order
    # would leave a review marked done while the execution still waits — invisible,
    # and it would eventually time out and dead-letter a document a human had
    # already fixed.
    store.mark_completed(correlation_id, correction["reviewer_id"])

    logger.info(
        json.dumps(
            {
                "event": "correction_submitted",
                "correlation_id": correlation_id,
                "reviewer_id": correction["reviewer_id"],
                "original_class": pending.get("predicted_class"),
                "corrected_class": correction["corrected_class"],
                "prediction_was_correct": output["prediction_was_correct"],
                "review_reason": pending.get("review_reason"),
            }
        )
    )

    return {
        "correlation_id": correlation_id,
        "status": "RESUMED",
        "prediction_was_correct": output["prediction_was_correct"],
    }


# ---------------------------------------------------------------------------
# AWS adapters. Kept at the edge so the logic above needs no AWS to test.
# ---------------------------------------------------------------------------


class DynamoReviewStore:
    """ReviewStore backed by the DynamoDB review queue table."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def get_pending(self, correlation_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"correlation_id": correlation_id})
        item = response.get("Item")
        if not item or item.get("status") != "PENDING_REVIEW":
            return None
        return dict(item)

    def mark_completed(self, correlation_id: str, reviewer_id: str) -> None:
        self._table.update_item(
            Key={"correlation_id": correlation_id},
            UpdateExpression="SET #s = :done, reviewed_by = :who",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":done": "REVIEWED",
                ":who": reviewer_id,
            },
            # Only a still-pending review may be completed, so two reviewers racing
            # on the same document cannot both succeed.
            ConditionExpression="#s = :pending",
        )


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """API Gateway entrypoint for POST /reviews/{correlation_id}/corrections."""
    import os

    import boto3

    table_name = os.environ["REVIEW_QUEUE_TABLE"]
    dynamodb = boto3.resource("dynamodb")
    store = DynamoReviewStore(dynamodb.Table(table_name))
    stepfunctions = boto3.client("stepfunctions")

    raw_body = event.get("body") or "{}"
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as error:
        return _response(400, {"error": f"body is not valid JSON: {error}"})

    try:
        result = submit_correction(payload, store=store, stepfunctions=stepfunctions)
    except ReviewApiError as error:
        return _response(error.status, {"error": error.message})
    except Exception as error:  # noqa: BLE001
        logger.exception("unhandled error in review api")
        return _response(500, {"error": f"internal error: {type(error).__name__}"})

    return _response(200, result)
