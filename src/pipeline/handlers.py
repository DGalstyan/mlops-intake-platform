"""Lambda handlers for the two functions the intake workflow genuinely needs.

Both are justified in one line each, per the assignment's requirement that retained
Lambdas earn their place:

- `normalize_ocr_handler` — Textract returns a block *graph*; assembling
  reading-order text from it is a real transformation, not a field mapping, and ASL
  has no way to sort and join an array of geometry-bearing objects.
- `validate_handler` — JSON Schema validation and cross-field rules cannot be
  expressed in ASL. A Choice state compares two values; it cannot evaluate a regex,
  an enum, or "expiry_date must be after date_of_birth".

Everything else in the workflow uses direct SDK integrations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.pipeline.validate import validate_document  # noqa: E402

logger = logging.getLogger("intake.pipeline")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# Two LINE blocks whose vertical centres are within this fraction of the page
# height are treated as the same visual row and ordered left-to-right. Textract
# returns blocks in an order that is usually but not reliably reading order,
# especially for multi-column layouts.
ROW_BAND_TOLERANCE: Final[float] = 0.01


def log_event(event: str, **fields: Any) -> None:
    """Structured JSON logging with correlation_id, from the first milestone that
    writes logs — M4 traces on this field, and retrofitting it means reprocessing
    every stored log line."""
    logger.info(json.dumps({"event": event, **fields}, default=str))


# ---------------------------------------------------------------------------
# Lambda 1: OCR normalisation
# ---------------------------------------------------------------------------


def assemble_reading_order(blocks: list[dict[str, Any]]) -> list[str]:
    """Extract LINE text in reading order.

    Sorted by vertical band then horizontal position rather than by Textract's
    returned order, which is not guaranteed to be reading order for multi-column
    documents. Getting this wrong interleaves two columns into nonsense — which a
    bag-of-words classifier is largely immune to, but which lands verbatim in the
    Bedrock extraction prompt and in the text a human reviewer reads.
    """
    lines: list[tuple[float, float, str]] = []
    for block in blocks:
        if block.get("BlockType") != "LINE":
            continue
        text = block.get("Text")
        if not text:
            continue
        box = block.get("Geometry", {}).get("BoundingBox", {})
        top = float(box.get("Top", 0.0))
        left = float(box.get("Left", 0.0))
        height = float(box.get("Height", 0.0))
        lines.append((top + height / 2.0, left, text))

    if not lines:
        return []

    lines.sort(key=lambda item: (item[0], item[1]))

    # Band lines into rows so a slightly-higher neighbour on the same visual line
    # does not sort above a whole column.
    ordered: list[str] = []
    band: list[tuple[float, float, str]] = [lines[0]]
    for entry in lines[1:]:
        if abs(entry[0] - band[0][0]) <= ROW_BAND_TOLERANCE:
            band.append(entry)
        else:
            band.sort(key=lambda item: item[1])
            ordered.extend(text for _, _, text in band)
            band = [entry]
    band.sort(key=lambda item: item[1])
    ordered.extend(text for _, _, text in band)
    return ordered


def normalize_ocr_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Turn Textract blocks into text plus the metadata downstream states need."""
    correlation_id = event.get("correlation_id", "unknown")
    blocks = event.get("blocks") or []

    lines = assemble_reading_order(blocks)
    text = "\n".join(lines)

    # Content hash over the *extracted text*, not the source bytes. Two scans of the
    # same page produce different bytes but the same text, and for deduplication
    # purposes they are the same document. Not used as the primary idempotency key
    # (that is bucket#key#versionId, which is cheaper because it needs no OCR) but
    # recorded so content-level duplicates are detectable after the fact.
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    result = {
        "text": text,
        "content_sha256": f"sha256:{content_sha256}",
        "char_count": len(text),
        "line_count": len(lines),
    }
    log_event(
        "ocr_normalized",
        correlation_id=correlation_id,
        char_count=result["char_count"],
        line_count=result["line_count"],
        block_count=len(blocks),
    )
    return result


# ---------------------------------------------------------------------------
# Lambda 2: extraction validation
# ---------------------------------------------------------------------------


class ModelOutputError(ValueError):
    """The model's response could not be parsed into a JSON object."""


def parse_model_json(model_output: Any) -> dict[str, Any]:
    """Pull the JSON object out of a Bedrock Messages API response.

    Tolerant of the two things models reliably do despite instructions: wrapping the
    object in a markdown fence, and adding a sentence before or after it. Tolerated
    rather than rejected because the alternative is sending an otherwise-good
    extraction to a human for a formatting quirk.

    NOT tolerant of anything that is not ultimately a JSON object — a model that
    returns prose has failed, and pretending otherwise would auto-approve an empty
    field set.
    """
    if isinstance(model_output, str):
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError as error:
            raise ModelOutputError(
                f"model output is neither JSON nor a response envelope: {error}"
            ) from error

    if not isinstance(model_output, dict):
        raise ModelOutputError(
            f"expected a response object, got {type(model_output).__name__}"
        )

    # Anthropic Messages API shape: {"content": [{"type": "text", "text": "..."}]}
    text: str | None = None
    content = model_output.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text", ""))
                break
    elif isinstance(model_output.get("completion"), str):
        # Older text-completion shape, kept so a model-id change does not break
        # parsing silently.
        text = model_output["completion"]

    if text is None:
        # The response may already be the extracted object itself, which is what
        # happens in tests and with a structured-output model.
        if any(key not in {"usage", "stop_reason", "id", "model"} for key in model_output):
            return model_output
        raise ModelOutputError("no text content found in the model response")

    candidate = text.strip()
    if candidate.startswith("```"):
        # Strip a markdown fence, with or without a language tag.
        candidate = candidate.split("```", 2)[1] if "```" in candidate[3:] else candidate[3:]
        if candidate.startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip("` \n")

    # Take the outermost JSON object if the model added surrounding prose.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ModelOutputError(
            f"no JSON object found in model text: {candidate[:200]!r}"
        )

    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as error:
        raise ModelOutputError(f"model text is not valid JSON: {error}") from error

    if not isinstance(parsed, dict):
        raise ModelOutputError(
            f"model returned a {type(parsed).__name__}, not an object"
        )
    return parsed


def validate_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Parse the model's output, then validate it against the class schema.

    Never raises for a *content* problem. A malformed model response is a validation
    failure that sends the document to human review, not a Lambda error that
    dead-letters it — a reviewer can read the document and type the fields, which is
    strictly better than discarding it.
    """
    correlation_id = event.get("correlation_id", "unknown")
    document_class = event["document_class"]

    schema_raw = event["response_schema"]
    schema = json.loads(schema_raw) if isinstance(schema_raw, str) else schema_raw

    try:
        fields = parse_model_json(event.get("model_output"))
    except ModelOutputError as error:
        log_event(
            "extraction_unparseable",
            correlation_id=correlation_id,
            document_class=document_class,
            error=str(error),
        )
        return {
            "fields": {},
            "validation": {
                "valid": False,
                "issue_count": 1,
                "issues": [
                    {
                        "field": "<response>",
                        "code": "unparseable_model_output",
                        "message": str(error),
                    }
                ],
                "failed_fields": ["<response>"],
            },
        }

    result = validate_document(fields, schema, document_class)
    log_event(
        "extraction_validated",
        correlation_id=correlation_id,
        document_class=document_class,
        valid=result.valid,
        issue_count=len(result.issues),
        failed_fields=sorted({issue.field for issue in result.issues}),
    )
    return {"fields": fields, "validation": result.to_dict()}
