"""SageMaker inference handlers.

The four-function contract (`model_fn`, `input_fn`, `predict_fn`, `output_fn`) is
kept free of any HTTP concern so it can be unit-tested directly, and so the same
handlers work whether they are driven by this repo's own serving layer
(`serve.py`) or by a managed SageMaker framework container.

The response shape is a **contract with the Step Functions intake workflow**, not
an implementation detail. M3's Route state reads `confidence` and compares it to a
threshold; M4 emits metrics from these fields; M5's drift job parses them out of
data-capture records. Changing a field name here breaks all three, which is why
`RESPONSE_SCHEMA_VERSION` exists and why there is a contract test asserting the
exact key set.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    AUTO_APPROVE_CONFIDENCE_THRESHOLD,
    DOCUMENT_CLASSES,
    MODEL_FILENAME,
)
from src.training.model import DocumentClassifier, load_classifier  # noqa: E402

# Bump on any change to the response body's shape. Consumers should refuse a
# major version they do not understand rather than silently mis-read fields.
RESPONSE_SCHEMA_VERSION: Final[str] = "1.0.0"

CONTENT_TYPE_JSON: Final[str] = "application/json"

# Guards against a pathological payload exhausting endpoint memory. OCR output for
# a long document is realistically tens of kilobytes; 1 MB is generous headroom
# and still bounded.
MAX_TEXT_CHARS: Final[int] = 1_000_000
MAX_BATCH_SIZE: Final[int] = 100


class InferenceError(ValueError):
    """A client-side problem: malformed payload, wrong content type, too large.

    Distinguished from an internal failure because the serving layer maps this to
    4xx and everything else to 5xx. That distinction matters more than it looks:
    the endpoint's 5xx alarm drives the M2 auto-rollback, so classifying a
    malformed client request as a server error would roll back a perfectly good
    deployment because someone posted bad JSON.
    """


def model_fn(model_dir: str) -> DocumentClassifier:
    """Load the model. Called once per container at startup.

    Dispatches through `load_classifier`, so the artifact's recorded
    implementation decides which class loads it — a model trained by a future
    implementation is not silently misread by this one.
    """
    path = Path(model_dir) / MODEL_FILENAME
    if not path.is_file():
        # Listing the directory in the error is deliberate: the usual cause is a
        # model.tar.gz packed with an unexpected internal layout, and the
        # directory contents identify that immediately.
        available = (
            sorted(p.name for p in Path(model_dir).iterdir())
            if Path(model_dir).is_dir()
            else ["<model_dir does not exist>"]
        )
        raise FileNotFoundError(
            f"no model artifact at {path}. Contents of {model_dir}: {available}"
        )
    return load_classifier(path)


def input_fn(request_body: str | bytes, content_type: str = CONTENT_TYPE_JSON) -> list[str]:
    """Parse a request into a list of document texts.

    Accepts either a single document or a batch:

        {"text": "..."}                     -> one document
        {"texts": ["...", "..."]}           -> a batch
        {"instances": [{"text": "..."}]}    -> a batch, SageMaker's convention

    Validation is strict and explicit. A permissive parser that coerced junk into
    an empty string would return a confident prediction for a document that was
    never really submitted, and the Route state would auto-approve it.
    """
    if content_type.split(";")[0].strip() != CONTENT_TYPE_JSON:
        raise InferenceError(
            f"unsupported content type {content_type!r}; expected {CONTENT_TYPE_JSON}"
        )

    if isinstance(request_body, bytes):
        try:
            request_body = request_body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InferenceError(f"request body is not valid UTF-8: {error}") from error

    try:
        payload = json.loads(request_body)
    except json.JSONDecodeError as error:
        raise InferenceError(f"request body is not valid JSON: {error}") from error

    if not isinstance(payload, dict):
        raise InferenceError(
            f"expected a JSON object, got {type(payload).__name__}"
        )

    texts: list[Any]
    if "text" in payload:
        texts = [payload["text"]]
    elif "texts" in payload:
        if not isinstance(payload["texts"], list):
            raise InferenceError("'texts' must be a list")
        texts = list(payload["texts"])
    elif "instances" in payload:
        if not isinstance(payload["instances"], list):
            raise InferenceError("'instances' must be a list")
        texts = []
        for i, instance in enumerate(payload["instances"]):
            if not isinstance(instance, dict) or "text" not in instance:
                raise InferenceError(
                    f"instances[{i}] must be an object containing 'text'"
                )
            texts.append(instance["text"])
    else:
        raise InferenceError(
            "payload must contain one of 'text', 'texts' or 'instances'"
        )

    if not texts:
        raise InferenceError("no documents in request")
    if len(texts) > MAX_BATCH_SIZE:
        raise InferenceError(
            f"batch of {len(texts)} exceeds the maximum of {MAX_BATCH_SIZE}"
        )

    validated: list[str] = []
    for i, text in enumerate(texts):
        if not isinstance(text, str):
            raise InferenceError(
                f"document {i} must be a string, got {type(text).__name__}"
            )
        if not text.strip():
            raise InferenceError(f"document {i} is empty or whitespace only")
        if len(text) > MAX_TEXT_CHARS:
            raise InferenceError(
                f"document {i} is {len(text)} characters, over the "
                f"{MAX_TEXT_CHARS} limit"
            )
        validated.append(text)
    return validated


def predict_fn(texts: list[str], model: DocumentClassifier) -> list[dict[str, Any]]:
    """Classify documents, returning one result per input in input order.

    Emits the full per-class probability distribution alongside the top label, not
    just the winner. M5's prediction-drift detection needs the distribution, and
    recovering it later from data-capture is impossible if it was never returned.
    """
    proba = model.predict_proba(texts)
    model_classes = list(model.classes)

    results: list[dict[str, Any]] = []
    for row_index in range(proba.shape[0]):
        row = proba[row_index]
        distribution = {
            label: float(row[column]) for column, label in enumerate(model_classes)
        }
        # Canonical order, so consumers can rely on key presence for every class
        # even if a future model orders its columns differently.
        ordered = {
            label: distribution.get(label, 0.0) for label in DOCUMENT_CLASSES
        }
        top_label = max(ordered, key=lambda label: ordered[label])
        confidence = ordered[top_label]

        results.append(
            {
                "predicted_class": top_label,
                "confidence": confidence,
                "class_probabilities": ordered,
                # Computed here rather than in the state machine so the threshold
                # and the probability that it is compared against can never drift
                # apart across two codebases. M3 may still override the routing
                # decision on business rules; this is the model's own view.
                "auto_approve_eligible": confidence
                >= AUTO_APPROVE_CONFIDENCE_THRESHOLD,
                "confidence_threshold": AUTO_APPROVE_CONFIDENCE_THRESHOLD,
            }
        )
    return results


def output_fn(
    predictions: list[dict[str, Any]], accept: str = CONTENT_TYPE_JSON
) -> tuple[str, str]:
    """Serialise the response. Returns (body, content_type)."""
    if accept and accept != "*/*":
        if accept.split(";")[0].strip() != CONTENT_TYPE_JSON:
            raise InferenceError(
                f"unsupported accept type {accept!r}; expected {CONTENT_TYPE_JSON}"
            )

    body = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "predictions": predictions,
        "model_version": os.environ.get("MODEL_VERSION", "unknown"),
    }
    return json.dumps(body), CONTENT_TYPE_JSON
