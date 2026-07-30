# M3 evidence — intake traces

**These traces come from a local simulation, not from a Step Functions execution.**
M3's deliverable properly means an execution history from a real account, which needs
AWS credentials. Read the caveat below before treating these as the deliverable.

## What is here

| File | Shows |
|---|---|
| `trace-auto-approved.json` | A document classified confidently, extracted, validated, and auto-approved without human involvement. |
| `trace-human-corrected.json` | A `medical_report` sent to review by business rule, corrected by a reviewer through the review API, resumed via task token, and persisted as labelled training data. |
| `trace-duplicate-delivery.json` | The same S3 object delivered twice. The second delivery short-circuits at the idempotency claim. |
| `trace-schema-failure.json` | An extraction that is schema-valid JSON but breaks a cross-field rule (`expiry_date` before `date_of_birth`), routed to review as `SCHEMA_VALIDATION_FAILED`. |
| `simulation-summary.json` | Table counts after the run. |

## The two required paths

**Auto-approved:**
```
Prepare -> ClaimIdempotencyKey -> ExtractText -> NormalizeOcr -> CheckEmptyDocument
-> Classify -> Route -> FetchExtractionPrompt -> Extract -> ValidateExtraction
-> DecideOutcome -> AutoApprove -> MarkLedgerComplete -> Succeed
```

**Human-corrected:**
```
... -> DecideOutcome -> CreateReviewTask (waits on task token)
-> ReviewApi.submitCorrection (out-of-band HTTP call)
-> CreateReviewTask.resumed -> PersistCorrection -> StoreReviewedResult
-> MarkLedgerComplete -> Succeed
```

## Idempotency, demonstrated

Four documents were run, one of them a redelivery of the first:

- `results_written: 3` — not 4. The duplicate produced no second result.
- `review_tasks_created: 2` — one document, one review task.
- The duplicate's trace is exactly `Prepare -> ClaimIdempotencyKey -> DuplicateDelivery`.

That last point is the cost argument for claiming the idempotency key *first*: a
duplicate delivery costs one conditional DynamoDB write, not a Textract call, an
endpoint invocation and a Bedrock call. It also never reaches `CreateReviewTask`, so
a redelivery cannot waste a reviewer's time.

## What is real in this simulation, and what is not

**Real** — the code that runs here is the code that deploys:
- the trained classifier and its confidence
- `normalize_ocr_handler`, including reading-order assembly and the content hash
- `parse_model_json`, `validate_document`, and every cross-field rule
- `submit_correction`, including task-token lookup and correction validation
- the routing conditions and their evaluation order
- the idempotency ledger semantics

**Stubbed:**
- Textract (synthetic LINE blocks, deliberately emitted out of order so the
  reading-order sort is genuinely exercised)
- Bedrock (a scripted extractor with `good` / `invalid` / `prose` modes)
- DynamoDB (in-memory dicts)
- Step Functions itself (`simulate_intake.py` walks the states)

## Why this is worth anything

A simulator that routed differently from the deployed state machine would prove
nothing. `tests/test_pipeline.py::TestSimulatorMatchesAsl` asserts that the
simulator and `statemachines/intake.asl.json` agree on:

- which classes are always-review,
- the three review-reason markers,
- the *order* in which `DecideOutcome` evaluates its conditions — which decides
  which reason a document is attributed to, and therefore whether M4's breakdown of
  why documents go to review is correct,
- that the ASL reads the endpoint's `auto_approve_eligible` flag rather than
  hardcoding a threshold.

Separately, `tests/test_asl.py` (37 tests) asserts the invariants the design depends
on directly against the ASL: Retry with FULL jitter on every Task, a `States.ALL`
catcher last on every Task, every catcher preserving document context, conditional
writes on both the result and the review task, a timeout on the review state, and
that the dead-letter path only reads fields guaranteed to exist.

## What is still missing

- **A real Step Functions execution history.** Needs credentials, a deployed
  endpoint, and the M3 infrastructure applied.
- **The M3 Terraform.** The state machine, the five DynamoDB tables, the three
  Lambda functions, the EventBridge rule, the review API Gateway and the SQS
  dead-letter queue are **not yet written**. The ASL and handler code exist and are
  tested; nothing deploys them.
- **A real duplicate-delivery test against S3 + EventBridge.** The idempotency logic
  is proven in simulation; the execution-name dedupe that sits in front of it is not.

Reproduce with `make simulate-intake`.
