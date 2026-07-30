# M3 — Orchestration & Human-in-the-Loop

**Grade tie-in:** feeds all areas; enables the
end-to-end trace

## Goal
End-to-end trace of one **auto-approved** document and one **human-corrected**
document.

## Tasks
- [ ] Intake state machine: OCR (Textract) → Classify (SageMaker Runtime) → Route
      (confidence + business rule) → Extract (Bedrock, class prompt + JSON schema)
      → Validate (schema + field rules) → Auto-approve / Human review.
- [ ] Prefer **direct SDK integrations**; justify every retained Lambda in one line.
- [ ] `Retry` (with jitter) + `Catch` on **every** fallible state; Bedrock/Textract
      throttling never loses a document.
- [ ] **Idempotency**: same S3 object twice → one result, one review task
      (deterministic key + conditional write / execution-name dedupe).
- [ ] Human review via `.waitForTaskToken`: low-confidence or schema-failing docs
      park in a DynamoDB review queue; reviewer submits via a small API; token
      resumes the workflow; handle timeout/heartbeat.
- [ ] Corrections written back as **labelled data** (reviewer id, timestamp,
      original prediction, corrected label, doc ref).
- [ ] Dead-letter path with enough context to debug one document.
- [ ] Propagate `correlation_id` through every state and into Bedrock metadata.

## Acceptance criteria (Deliverable)
- [ ] Trace of one auto-approved doc AND one human-corrected doc in `evidence/`.
- [ ] Duplicate-delivery test proves idempotency (no double result / double task).
- [ ] Corrections land in the labelled-data store with full provenance.

## Definition of done
A rubric audit confirms retries/catches on all fallible states, idempotency,
working task-token resume, and a debuggable dead-letter path.
