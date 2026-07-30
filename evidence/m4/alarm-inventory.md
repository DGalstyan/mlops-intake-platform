# M4 alarm inventory

**Generated from the Terraform source** by `make alarm-inventory` — not
hand-maintained, and not read from `terraform output`. An output-based
inventory would only exist after an apply and would describe whatever was last
applied rather than what is in the repo. A test asserts every
`aws_cloudwatch_metric_alarm` in `infra/` appears here, so adding an alarm
without documenting it fails CI.

**Nothing here is deployed.** These are the alarms that would be created.
Names are shown resolved for the `dev` environment; `staging` differs only in
that suffix.

## The distinction that matters

The `measures` column separates **model quality** from **system health**. Every
system-health alarm can be green while the model is quietly wrong — which is the
whole reason the model-quality proxies exist. None of them measures accuracy:
there is no ground truth in production.

**10 alarms**, 2 in `endpoint`, 1 in `intake`, 7 in `observability`.

| Alarm | Measures | Module |
|---|---|---|
| `intake-endpoint-5xx-dev` | system health (drives auto-rollback) | `endpoint` |
| `intake-endpoint-latency-dev` | system health (drives auto-rollback) | `endpoint` |
| `intake-dev-dlq-not-empty` | data safety | `intake` |
| `intake-dev-auto-approval-rate-low` | model quality (indirect) | `observability` |
| `intake-dev-confidence-p10-low` | model quality (concept-drift proxy) | `observability` |
| `intake-dev-cost-per-document-high` | cost | `observability` |
| `intake-dev-intake-latency-p95-high` | system health | `observability` |
| `intake-dev-intake-executions-failed` | system health | `observability` |
| `intake-dev-human-override-rate-high` | model quality (primary proxy) | `observability` |
| `intake-dev-schema-failure-rate-high` | model quality (extraction) | `observability` |

---

## `endpoint` — release safety — drives the canary auto-rollback

### `intake-endpoint-5xx-dev`

- **Measures:** system health (drives auto-rollback)
- **Fires when:** Endpoint returned 5xx errors. Drives automatic rollback of an in-flight deployment. Note the serving layer returns 4xx for malformed client input, so a 5xx here is a genuine endpoint fault, not a bad request.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-endpoint-latency-dev`

- **Measures:** system health (drives auto-rollback)
- **Fires when:** Endpoint p99 model latency exceeded its ceiling. Drives automatic rollback. Catches a variant that is functional but unusably slow — the failure an error-rate alarm alone never sees.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

## `intake` — data safety — dead-letter queue

### `intake-dev-dlq-not-empty`

- **Measures:** data safety
- **Fires when:** One or more documents failed intake and are sitting in the dead-letter queue.
- **What breaks:** those documents have no result and no review task. They are not lost — the queue retains them for 14 days — but they are not processed either.
- **First response:** read one message. Each carries correlation_id, the failing state, the error cause and a pointer to the source object, which is enough to diagnose without re-running. Fix forward, then replay the queue.
- **Note:** review tasks that time out after 7 days also land here, and those are documents a human was meant to look at — check the failing state before assuming a technical fault.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

## `observability` — platform metrics — business, model and pipeline health

### `intake-dev-auto-approval-rate-low`

- **Measures:** model quality (indirect)
- **Fires when:** Auto-approval rate fell below ${var.auto_approval_rate_floor_percent}%.
- **What breaks:** more documents are going to humans, so the review queue grows and throughput falls. The pipeline is healthy — this is a MODEL or INPUT signal.
- **First response:** check ConfidenceP10 and the per-class breakdown on the dashboard. If confidence sank, suspect drift or a bad deploy; if only one class moved, suspect a changed document layout from one sender.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-dev-confidence-p10-low`

- **Measures:** model quality (concept-drift proxy)
- **Fires when:** 10th-percentile classifier confidence fell below ${var.confidence_p10_floor}.
- **What breaks:** the low-confidence tail is growing, so more documents route to humans. This is the CONCEPT-DRIFT PROXY: if confidence decays while input length and predicted-class distribution look unchanged, the world moved in a way the features do not capture.
- **First response:** run the M5 drift report. Compare input and prediction distributions against the M1 baseline before concluding the model degraded — 'the data changed' and 'the model got worse' need different responses.
- **Why this statistic:** the tail moves first. By the time the median sags, a large share of traffic is already going to review.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-dev-cost-per-document-high`

- **Measures:** cost
- **Fires when:** Estimated LLM cost per document exceeded $${var.estimated_cost_per_document_ceiling_usd}. Computed as metric math over real emitted token counts times the prices in config/prices.json (retrieved ${local.prices.retrieved}).
- **What breaks:** spend, not correctness.
- **First response:** check LLMInputTokens per document first. A jump there means either the OCR text grew (scanned images with more noise) or the prompt grew. A jump in OUTPUT tokens usually means the model started explaining itself, which also breaks JSON parsing — cross-check the schema-failure rate.
- **Note:** this covers Bedrock only. Textract and the endpoint's standing hourly charge are on the dashboard's cost row but are not per-document.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-dev-intake-latency-p95-high`

- **Measures:** system health
- **Fires when:** p95 end-to-end intake time exceeded ${var.end_to_end_latency_p95_seconds}s.
- **What breaks:** documents take longer to reach a result. Usually retries against a throttling service rather than one slow stage.
- **First response:** the dashboard's per-stage panel (from X-Ray) shows which stage grew. Textract and Bedrock throttling are the usual causes and are self-healing; a rise in the endpoint's ModelLatency is not.
- **Caveat:** this statistic includes documents that waited for a human, which can be days. Read it alongside the auto-approval rate — a rise here with a falling auto-approval rate is a routing change, not a performance problem.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-dev-intake-executions-failed`

- **Measures:** system health
- **Fires when:** Intake executions are failing outright.
- **What breaks:** documents are not being processed. Each failure should have a corresponding dead-letter message.
- **First response:** read one dead-letter message — it carries correlation_id, the failing state and the error cause. Fix forward, then replay the queue.
- **Note:** duplicate deliveries end in Succeed by design, so they do not appear here.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-dev-human-override-rate-high`

- **Measures:** model quality (primary proxy)
- **Fires when:** Reviewers are overriding more than ${var.human_override_rate_ceiling_percent}% of the documents they see. This is the platform's PRIMARY production model-quality proxy — the closest available signal to 'accuracy fell'.
- **What breaks:** nothing operationally; the model is getting the reviewed slice wrong more often.
- **First response:** pull the corrections table for the affected class and compare against the golden set. This is the trigger for considering a retrain.
- **Caveat:** the denominator is only documents humans SAW — low-confidence and always-review classes. It is blind to confidently-wrong documents, which is why it cannot be treated as an accuracy measurement.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

### `intake-dev-schema-failure-rate-high`

- **Measures:** model quality (extraction)
- **Fires when:** Extraction output failed schema or field-rule validation on more than ${var.schema_failure_rate_ceiling_percent}% of documents.
- **What breaks:** those documents go to human review instead of auto-approving. This is an EXTRACTION-MODEL signal, not a pipeline one — the pipeline is working correctly when this fires.
- **First response:** the dashboard's failed-field breakdown names which field is failing. A single field failing across one class usually means a changed document layout; every field failing usually means a prompt or model change.
- **Notifies:** the platform SNS topic (`intake-<env>-alarms`). No email is subscribed by default — an unconfirmed email subscription looks configured while delivering nothing.

---

## Who is paged

Honest answer: **nobody, yet.** Every alarm publishes to one SNS topic with no
subscriber by default. `alarm_email` adds one, but a real rotation needs an
on-call tool and an escalation policy, and inventing a paging story this
take-home does not implement would be worse than saying so.

What the topic separation *would* be: the model-quality alarms are not
wake-someone-up events — they need a human with the corrections table and a day
to think. The pipeline-health and dead-letter alarms are. Splitting into two
topics is the first thing to do when a rotation exists.

