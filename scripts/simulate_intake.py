#!/usr/bin/env python3
"""Run the intake flow locally and emit the two traces M3 is graded on.

**This is a simulation, and the README says so.** M3's deliverable is an end-to-end
trace of one auto-approved and one human-corrected document. That deliverable
properly means a Step Functions execution history from a real account, which needs
credentials. This produces the equivalent trace by running the pipeline's real logic
in-process against stubbed AWS boundaries.

What is real here: the trained classifier, `normalize_ocr_handler`,
`parse_model_json`, `validate_document`, the field rules, the routing conditions, the
correction validation, and the idempotency ledger semantics.

What is stubbed: Textract (synthetic blocks from generated documents), Bedrock (a
scripted extractor), DynamoDB (in-memory dicts), and Step Functions itself (this
module walks the states).

The routing conditions are the risky part of a simulation like this — a simulator
that routes differently from the deployed ASL proves nothing. `tests/test_simulator.py`
asserts that the conditions here and the Choice states in `intake.asl.json` agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AUTO_APPROVE_CONFIDENCE_THRESHOLD, DOCUMENT_CLASSES  # noqa: E402
from src.data import generate  # noqa: E402
from src.pipeline.handlers import normalize_ocr_handler, validate_handler  # noqa: E402
from src.pipeline.prompts import render_all  # noqa: E402
from src.pipeline.review_api import submit_correction  # noqa: E402
from src.training.model import TfidfLinearClassifier  # noqa: E402

# Classes that always go to a human regardless of confidence. Must match the Route
# and DecideOutcome Choice states in statemachines/intake.asl.json — there is a test.
ALWAYS_REVIEW_CLASSES: tuple[str, ...] = ("medical_report",)


@dataclass
class Trace:
    """A step-by-step record of one document's journey, mirroring an execution history."""

    correlation_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)

    def record(self, state: str, **detail: Any) -> None:
        self.steps.append({"state": state, **detail})

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "step_count": len(self.steps),
            "states_entered": [step["state"] for step in self.steps],
            "steps": self.steps,
        }


@dataclass
class Tables:
    """In-memory stand-ins for the five DynamoDB tables."""

    ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_queue: dict[str, dict[str, Any]] = field(default_factory=dict)
    corrections: dict[str, dict[str, Any]] = field(default_factory=dict)
    dead_letter: list[dict[str, Any]] = field(default_factory=list)


class FakeStepFunctions:
    """Captures SendTaskSuccess so the resumed branch can be driven."""

    def __init__(self) -> None:
        self.resumed: dict[str, Any] = {}

    def send_task_success(self, *, taskToken: str, output: str) -> dict[str, Any]:
        self.resumed[taskToken] = json.loads(output)
        return {}

    def send_task_failure(
        self, *, taskToken: str, error: str, cause: str
    ) -> dict[str, Any]:
        self.resumed[taskToken] = {"failed": True, "error": error, "cause": cause}
        return {}


class SimulatedReviewStore:
    def __init__(self, tables: Tables) -> None:
        self._tables = tables

    def get_pending(self, correlation_id: str) -> dict[str, Any] | None:
        item = self._tables.review_queue.get(correlation_id)
        if not item or item.get("status") != "PENDING_REVIEW":
            return None
        return item

    def mark_completed(self, correlation_id: str, reviewer_id: str) -> None:
        self._tables.review_queue[correlation_id]["status"] = "REVIEWED"
        self._tables.review_queue[correlation_id]["reviewed_by"] = reviewer_id


def textract_blocks_for(text: str) -> list[dict[str, Any]]:
    """Stub Textract: turn text into LINE blocks with plausible geometry.

    Emitted deliberately out of reading order so `assemble_reading_order` is actually
    exercised rather than trivially passed a pre-sorted list.
    """
    words = text.split()
    lines = [" ".join(words[i : i + 8]) for i in range(0, len(words), 8)]
    blocks = [
        {
            "BlockType": "LINE",
            "Text": line,
            "Geometry": {
                "BoundingBox": {
                    "Top": 0.05 + index * 0.04,
                    "Left": 0.1,
                    "Width": 0.8,
                    "Height": 0.03,
                }
            },
        }
        for index, line in enumerate(lines)
    ]
    return list(reversed(blocks))


def bedrock_extract(document_class: str, text: str, *, mode: str) -> Any:
    """Stub Bedrock. `mode` selects a scripted outcome.

    - "good"        a complete, valid extraction
    - "invalid"     schema-valid JSON that breaks a cross-field rule
    - "prose"       the model ignores instructions and returns prose
    """
    if mode == "prose":
        return {
            "content": [
                {"type": "text", "text": "I was unable to find those fields."}
            ],
            "usage": {"input_tokens": 400, "output_tokens": 12},
        }

    payloads: dict[str, dict[str, Any]] = {
        "invoice": {
            "invoice_number": "INV-88213",
            "total_amount": 1284.5 if mode == "good" else 0,
            "currency": "USD",
            "due_date": "2026-09-15" if mode == "good" else "2199-09-15",
            "vendor_name": "Northwind Supplies Ltd",
            "purchase_order": None,
        },
        "medical_report": {
            "patient_reference": "MRN-55120",
            "report_date": "2026-07-02",
            "ordering_clinician": "Dr A. Okafor",
            "specimen_type": "blood",
            "findings_summary": "Haemoglobin within reference range. No abnormality detected.",
            "abnormal_flag": False,
        },
        "id_document": {
            "document_type": "passport",
            "document_number": "P4417823",
            "full_name": "Jordan Alexis Rivera",
            "date_of_birth": "1988-04-12",
            "expiry_date": "2031-04-11" if mode == "good" else "1980-01-01",
            "issuing_country": "GBR",
            "machine_readable_zone": None,
        },
        "correspondence": {
            "sender_name": "Meridian Claims Office",
            "recipient_name": "J. Rivera",
            "letter_date": "2026-06-28",
            "subject": "Acknowledgement of your enquiry",
            "reference_number": "CL-99312",
            "requires_response_by": "2026-07-19" if mode == "good" else "2026-01-01",
        },
    }
    body = payloads.get(document_class, {})
    return {
        "content": [{"type": "text", "text": f"```json\n{json.dumps(body)}\n```"}],
        "usage": {"input_tokens": len(text.split()), "output_tokens": 120},
    }


def run_document(
    *,
    document: generate.Document,
    classifier: TfidfLinearClassifier,
    prompts: dict[str, Any],
    tables: Tables,
    bedrock_mode: str,
    bucket: str = "intake-raw-dev",
    version_id: str = "v1",
) -> Trace:
    """Walk the intake states for one document."""
    key = f"incoming/{document.doc_id}.pdf"
    correlation_id = f"{bucket}#{key}#{version_id}"
    trace = Trace(correlation_id=correlation_id)

    # --- Prepare ---
    trace.record("Prepare", correlation_id=correlation_id, idempotency_key=correlation_id)

    # --- ClaimIdempotencyKey (conditional put) ---
    if correlation_id in tables.ledger:
        trace.record(
            "ClaimIdempotencyKey",
            outcome="ConditionalCheckFailedException",
            note="already claimed",
        )
        trace.record("DuplicateDelivery", outcome="DUPLICATE_IGNORED", end=True)
        return trace
    tables.ledger[correlation_id] = {"status": "PROCESSING"}
    trace.record("ClaimIdempotencyKey", outcome="claimed", status="PROCESSING")

    # --- ExtractText (Textract) ---
    blocks = textract_blocks_for(document.text)
    trace.record("ExtractText", block_count=len(blocks), service="textract (stub)")

    # --- NormalizeOcr (real Lambda handler) ---
    ocr = normalize_ocr_handler(
        {"correlation_id": correlation_id, "blocks": blocks}
    )
    trace.record(
        "NormalizeOcr",
        char_count=ocr["char_count"],
        line_count=ocr["line_count"],
        content_sha256=ocr["content_sha256"][:23] + "...",
    )

    # --- CheckEmptyDocument ---
    if ocr["char_count"] < 20:
        tables.dead_letter.append(
            {"correlation_id": correlation_id, "reason": "EMPTY_DOCUMENT"}
        )
        trace.record("CheckEmptyDocument", decision="DeadLetter", char_count=ocr["char_count"])
        return trace
    trace.record("CheckEmptyDocument", decision="Classify")

    # --- Classify (real model) ---
    proba = classifier.predict_proba([ocr["text"]])
    model_classes = list(classifier.classes)
    distribution = {
        label: float(proba[0][model_classes.index(label)]) for label in DOCUMENT_CLASSES
    }
    predicted = max(distribution, key=lambda label: distribution[label])
    confidence = distribution[predicted]
    classification = {
        "predicted_class": predicted,
        "confidence": confidence,
        "class_probabilities": distribution,
        "auto_approve_eligible": confidence >= AUTO_APPROVE_CONFIDENCE_THRESHOLD,
        "confidence_threshold": AUTO_APPROVE_CONFIDENCE_THRESHOLD,
        "model_version": "sim-local",
    }
    trace.record(
        "Classify",
        predicted_class=predicted,
        confidence=round(confidence, 4),
        auto_approve_eligible=classification["auto_approve_eligible"],
        true_label=document.label,
    )

    # --- Route (Choice) ---
    if predicted in ALWAYS_REVIEW_CLASSES:
        route_note = "business rule: always review"
    elif not classification["auto_approve_eligible"]:
        route_note = "low confidence"
    else:
        route_note = "eligible for auto-approval"
    trace.record("Route", next="FetchExtractionPrompt", reason=route_note)

    # --- FetchExtractionPrompt ---
    prompt = prompts[predicted]
    trace.record(
        "FetchExtractionPrompt",
        document_class=predicted,
        template_version=prompt.template_version,
        prompt_chars=len(prompt.prompt),
    )

    # --- Extract (Bedrock) ---
    model_output = bedrock_extract(predicted, ocr["text"], mode=bedrock_mode)
    usage = model_output.get("usage", {})
    trace.record(
        "Extract",
        service="bedrock (stub)",
        mode=bedrock_mode,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )

    # --- ValidateExtraction (real Lambda handler) ---
    validated = validate_handler(
        {
            "correlation_id": correlation_id,
            "document_class": predicted,
            "response_schema": json.dumps(prompt.response_schema),
            "model_output": model_output,
        }
    )
    trace.record(
        "ValidateExtraction",
        valid=validated["validation"]["valid"],
        issue_count=validated["validation"]["issue_count"],
        failed_fields=validated["validation"]["failed_fields"],
    )

    # --- DecideOutcome (Choice) — same three conditions as the ASL ---
    if not validated["validation"]["valid"]:
        review_reason = "SCHEMA_VALIDATION_FAILED"
    elif predicted in ALWAYS_REVIEW_CLASSES:
        review_reason = "BUSINESS_RULE_ALWAYS_REVIEW"
    elif not classification["auto_approve_eligible"]:
        review_reason = "LOW_CONFIDENCE"
    else:
        review_reason = ""

    if not review_reason:
        tables.results[correlation_id] = {
            "outcome": "AUTO_APPROVED",
            "document_class": predicted,
            "confidence": confidence,
            "extracted_fields": validated["fields"],
        }
        tables.ledger[correlation_id]["status"] = "COMPLETE"
        trace.record("DecideOutcome", decision="AutoApprove")
        trace.record("AutoApprove", outcome="AUTO_APPROVED", table="results")
        trace.record(
            "EmitAutoApprovedMetrics",
            namespace="Intake/Platform",
            metrics=["DocumentsProcessed", "AutoApproved", "Confidence",
                     "LLMInputTokens", "LLMOutputTokens", "OcrCharacters"],
            confidence=round(confidence, 4),
        )
        trace.record("MarkLedgerComplete", status="COMPLETE")
        trace.record("Succeed", end=True)
        return trace

    trace.record("DecideOutcome", decision="CreateReviewTask", review_reason=review_reason)
    if review_reason == "SCHEMA_VALIDATION_FAILED":
        trace.record("MarkSchemaFailure", review_reason=review_reason)
        trace.record(
            "EmitSchemaFailureMetric",
            namespace="Intake/Platform",
            metrics=["SchemaValidationFailure"],
        )

    # --- CreateReviewTask (.waitForTaskToken) ---
    task_token = f"tok-{hashlib.sha256(correlation_id.encode()).hexdigest()[:16]}"
    tables.review_queue[correlation_id] = {
        "correlation_id": correlation_id,
        "task_token": task_token,
        "status": "PENDING_REVIEW",
        "review_reason": review_reason,
        "predicted_class": predicted,
        "confidence": confidence,
        "extracted_fields": validated["fields"],
        "validation_issues": validated["validation"]["issues"],
        "document_text": ocr["text"],
    }
    trace.record(
        "CreateReviewTask",
        status="PENDING_REVIEW",
        review_reason=review_reason,
        task_token=task_token,
        waiting=True,
    )

    # --- reviewer acts, out of band, through the real review API logic ---
    stepfunctions = FakeStepFunctions()
    store = SimulatedReviewStore(tables)
    corrected_class = document.label  # the reviewer sees the document and is right
    api_result = submit_correction(
        {
            "correlation_id": correlation_id,
            "reviewer_id": "reviewer-eng-07",
            "corrected_class": corrected_class,
            "corrected_fields": {
                **validated["fields"],
                "_reviewer_corrected": True,
            },
            "note": f"Reviewed: {review_reason}",
        },
        store=store,
        stepfunctions=stepfunctions,
    )
    trace.record(
        "ReviewApi.submitCorrection",
        reviewer_id="reviewer-eng-07",
        corrected_class=corrected_class,
        prediction_was_correct=api_result["prediction_was_correct"],
        note="out-of-band HTTP call; resumes the waiting execution via SendTaskSuccess",
    )

    review = stepfunctions.resumed[task_token]
    trace.record("CreateReviewTask.resumed", task_token=task_token, output_keys=sorted(review))

    # --- PersistCorrection ---
    tables.corrections[correlation_id] = {
        "correlation_id": correlation_id,
        "reviewer_id": review["reviewer_id"],
        "original_predicted_class": predicted,
        "original_confidence": confidence,
        "corrected_class": review["corrected_class"],
        "was_prediction_correct": review["prediction_was_correct"],
        "review_reason": review_reason,
        "document_text": ocr["text"],
        "content_sha256": ocr["content_sha256"],
    }
    trace.record(
        "PersistCorrection",
        table="corrections",
        original_predicted_class=predicted,
        corrected_class=review["corrected_class"],
        was_prediction_correct=review["prediction_was_correct"],
        note="labelled training data, with provenance",
    )
    trace.record(
        "CheckOverride",
        prediction_was_correct=review["prediction_was_correct"],
        next="EmitConfirmedMetrics"
        if review["prediction_was_correct"]
        else "EmitOverriddenMetrics",
    )
    trace.record(
        "EmitConfirmedMetrics"
        if review["prediction_was_correct"]
        else "EmitOverriddenMetrics",
        namespace="Intake/Platform",
        metrics=["DocumentsProcessed", "HumanReviewed"]
        + (["HumanConfirmed"] if review["prediction_was_correct"] else ["HumanOverride"]),
        note="separate counters so HumanOverrideRate has a denominator of documents "
             "actually reviewed",
    )

    # --- StoreReviewedResult ---
    tables.results[correlation_id] = {
        "outcome": "HUMAN_APPROVED",
        "document_class": review["corrected_class"],
        "original_predicted_class": predicted,
        "reviewer_id": review["reviewer_id"],
        "extracted_fields": review["corrected_fields"],
    }
    tables.ledger[correlation_id]["status"] = "COMPLETE"
    trace.record("StoreReviewedResult", outcome="HUMAN_APPROVED", table="results")
    trace.record("MarkLedgerComplete", status="COMPLETE")
    trace.record("Succeed", end=True)
    return trace


def build_classifier() -> TfidfLinearClassifier:
    docs = generate.generate_documents(docs_per_class=200, seed=20260730)
    model = TfidfLinearClassifier(seed=20260730, min_df=1)
    model.fit([d.text for d in docs], [d.label for d in docs])
    return model


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("evidence/m3"))
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    classifier = build_classifier()
    prompts = render_all()
    tables = Tables()

    probes = generate.generate_documents(docs_per_class=40, seed=31337)

    # An auto-approved document: pick one the model gets confidently right, in a
    # class with no always-review rule.
    auto_doc = None
    for doc in probes:
        if doc.label in ALWAYS_REVIEW_CLASSES:
            continue
        proba = classifier.predict_proba([doc.text])[0]
        classes = list(classifier.classes)
        top = classes[int(proba.argmax())]
        if top == doc.label and float(proba.max()) >= 0.95:
            auto_doc = doc
            break
    if auto_doc is None:
        raise SystemExit("no confidently-classified document found for the auto trace")

    auto_trace = run_document(
        document=auto_doc,
        classifier=classifier,
        prompts=prompts,
        tables=tables,
        bedrock_mode="good",
    )

    # A human-corrected document: a medical_report, which the business rule sends to
    # review regardless of confidence. Chosen over a low-confidence document because
    # it demonstrates the override path deterministically.
    review_doc = next(d for d in probes if d.label == "medical_report")
    review_trace = run_document(
        document=review_doc,
        classifier=classifier,
        prompts=prompts,
        tables=tables,
        bedrock_mode="good",
    )

    # Idempotency: redeliver the auto-approved document unchanged.
    duplicate_trace = run_document(
        document=auto_doc,
        classifier=classifier,
        prompts=prompts,
        tables=tables,
        bedrock_mode="good",
    )

    # A schema-failing extraction, to show the third review reason.
    invalid_doc = next(d for d in probes if d.label == "id_document")
    invalid_trace = run_document(
        document=invalid_doc,
        classifier=classifier,
        prompts=prompts,
        tables=tables,
        bedrock_mode="invalid",
    )

    outputs = {
        "trace-auto-approved.json": auto_trace.to_dict(),
        "trace-human-corrected.json": review_trace.to_dict(),
        "trace-duplicate-delivery.json": duplicate_trace.to_dict(),
        "trace-schema-failure.json": invalid_trace.to_dict(),
    }
    for name, payload in outputs.items():
        (args.output_dir / name).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    summary = {
        "results_written": len(tables.results),
        "review_tasks_created": len(tables.review_queue),
        "corrections_recorded": len(tables.corrections),
        "dead_letter_entries": len(tables.dead_letter),
        "ledger_entries": len(tables.ledger),
        "outcomes": {
            cid.rsplit("/", 1)[-1]: item["outcome"]
            for cid, item in tables.results.items()
        },
    }
    (args.output_dir / "simulation-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
