# Runbook

Every alarm in this platform links here. One page, written for someone who did not
build it and is reading it at 3am.

> **Nothing in this platform has ever been deployed.** These procedures are derived
> from the configuration, not from an incident anyone has actually worked. Treat the
> commands as correct-by-construction and the *timings* as unverified.

---

## 0. First 60 seconds, whatever woke you

```bash
# What is on fire, in one view.
aws cloudwatch describe-alarms --state-value ALARM \
  --query 'MetricAlarms[].{name:AlarmName,since:StateUpdatedTimestamp,why:StateReason}' \
  --output table

# Is the endpoint even serving?
aws sagemaker describe-endpoint --endpoint-name intake-classifier-dev \
  --query '{status:EndpointStatus,lastOp:LastModifiedTime}'

# Are documents dead-lettering?
aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessagesVisible
```

**The single most useful triage question:** is this a *model* problem or a *system*
problem? The dashboard is arranged to answer that — section 2 (model health) and
section 3 (pipeline health) can move independently, and they need completely
different responses. Every alarm's `measures` tag says which it is.

---

## 1. "The endpoint is 5xx-ing at 3am"

The scenario the assignment asks about.

### Decide in this order

1. **Is a deployment in flight?**
   ```bash
   aws sagemaker describe-endpoint --endpoint-name intake-classifier-dev \
     --query 'EndpointStatus'
   ```
   `UPDATING` means a canary is shifting traffic. **Do nothing for 5 minutes.** The
   `intake-endpoint-5xx-dev` alarm is wired into `auto_rollback_configuration`, so a
   broken variant rolls itself back. Intervening manually during an automatic
   rollback is how you end up with a half-shifted endpoint.

2. **Did it already roll back?**
   ```bash
   aws sagemaker list-endpoint-configs --name-contains intake-classifier
   aws cloudtrail lookup-events --lookup-attributes \
     AttributeKey=EventName,AttributeValue=UpdateEndpoint --max-results 5
   ```
   If it rolled back, the incident is over. Find out *why* the variant was broken
   before redeploying — the smoke test (`make smoke-test`) is the fastest check.

3. **Is it 5xx or 4xx?** This distinction is load-bearing. The serving layer returns
   **4xx** for malformed input and **5xx** only for genuine faults, and only 5xx
   drives the rollback. A spike of 4xx means a caller is sending bad payloads, and the
   endpoint is fine.
   ```bash
   aws logs filter-log-pattern --log-group-name /aws/sagemaker/Endpoints/intake-classifier-dev \
     --filter-pattern '{ $.event = "bad_request" }' --max-items 20
   ```

4. **Is the model loaded at all?** `/ping` returns 503 — not 200 — when the model
   loaded but cannot predict. Look for `readiness_probe_failed` or
   `model_load_failed`:
   ```bash
   aws logs filter-log-pattern --log-group-name /aws/sagemaker/Endpoints/intake-classifier-dev \
     --filter-pattern '{ $.event = "model_load_failed" || $.event = "readiness_probe_failed" }'
   ```
   A `model_load_failed` with a version-mismatch message means the artifact was
   written by a different scikit-learn than the image has. That is what owning the
   inference image is supposed to prevent, so it also means something bypassed the
   normal build.

### If you must act

Roll back to the previous endpoint config by hand:
```bash
aws sagemaker update-endpoint --endpoint-name intake-classifier-dev \
  --endpoint-config-name <PREVIOUS_CONFIG_NAME>
```

**Documents are not lost while the endpoint is down.** The intake state machine
retries `SageMakerRuntime` errors 5 times with full jitter, and its `Catch` sends
anything that still fails to the dead-letter queue with full context. The backlog
becomes a drain-the-queue job, not a data-loss event. Fix the endpoint first, replay
second.

---

## 2. Documents are in the dead-letter queue

```bash
# Read one WITHOUT consuming it.
aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1 \
  --visibility-timeout 0 --query 'Messages[0].Body' --output text | jq .
```

Every message carries `correlation_id`, `execution_arn`, the failing state, the error
cause, and the source bucket/key/version. That is deliberately enough to diagnose
**without re-running the document**.

**Check the failing state before assuming a technical fault.** A review task that
timed out after 7 days lands here too, and that is a document a *human* was meant to
look at — the fix is a reviewer, not a code change.

### Replaying

There is no automated replay, and that is a gap (see the README). The manual path:
copy the source object to itself with a new version, which produces a new
`versionId`, therefore a new idempotency key, therefore a fresh execution:

```bash
aws s3 cp "s3://$BUCKET/$KEY" "s3://$BUCKET/$KEY" --metadata-directive REPLACE
```

Re-uploading the *same* version does nothing — the ledger claim rejects it. That is
the idempotency guarantee working, not a bug.

---

## 3. Auto-approval rate dropped

**This is a model or input signal. The pipeline is healthy.** Do not go looking at
latency.

1. **Check confidence p10 first** (dashboard section 2). If p10 sank, the model is
   less certain than it was.
2. **Then decide: did the data change, or did the model get worse?** These need
   opposite responses and the drift report is what distinguishes them. Running it is
   M5's job and is **not implemented yet** — until then, compare the per-class
   throughput panel against the baseline in `evidence/m1/v1-baseline-statistics.json`
   by hand.
3. **Check whether one class or all of them moved.** One class usually means a sender
   changed their document layout. All classes usually means a deploy.
4. **Check whether a deploy happened.** `git log` on the model package version, and
   the endpoint's config history.

**Do not raise the confidence threshold to make the number go up.** It will work, and
it will auto-approve documents the model is not confident about, and nothing will tell
you.

---

## 4. Human override rate is high

The primary model-quality proxy. Also the most easily misread number on the
dashboard.

**Before treating it as "the model got worse":** the denominator is only documents a
human *saw* — low-confidence ones and the always-review classes. That slice is
selected for being hard, so a high override rate on it is normal. What matters is a
**change** in the rate, not its level.

```bash
# What are reviewers actually changing?
aws dynamodb scan --table-name intake-dev-corrections \
  --filter-expression 'was_prediction_correct = :f' \
  --expression-attribute-values '{":f":{"BOOL":false}}' \
  --projection-expression 'original_predicted_class,corrected_class' \
  --max-items 50
```

A concentrated confusion pair (say `invoice` → `correspondence` repeatedly) is a
model problem worth retraining for. A scatter across all pairs is more likely
reviewer disagreement about ambiguous documents, which retraining will not fix and
may make worse.

**The blind spot, stated plainly:** this metric cannot see confidently-wrong
documents, because nobody reviews them. If a customer reports bad output while this
is green, that is the expected failure — see the README's accuracy-proxy section.

---

## 5. Schema validation failures are up

An **extraction-model** signal. The pipeline is working correctly.

```bash
# Which field is failing? The validator records failed_fields per document.
aws logs filter-log-pattern --log-group-name /aws/lambda/intake-dev-validate \
  --filter-pattern '{ $.event = "extraction_validated" && $.valid IS FALSE }' \
  --max-items 50
```

- **One field, one class** → that sender changed their layout, or OCR is failing on a
  specific region of the page.
- **Every field, all classes** → the prompt or the model changed. Check
  `template_version` in the prompts table against `PROMPT_TEMPLATE_VERSION` in
  `src/pipeline/prompts.py`.
- **`unparseable_model_output`** → the model is returning prose instead of JSON.
  Usually a model swap. Cross-check the cost panel: output tokens rise when the model
  starts explaining itself.

Prompts are data. Fixing one is `make seed-prompts`, not a deploy.

---

## 6. Cost per document is up

Look in this order — cheapest to check first:

1. **Input tokens per document** (dashboard section 4). A jump means the OCR text
   grew: noisier scans, or bigger documents.
2. **Output tokens per document.** A jump usually means the model is padding its
   answer, which also breaks JSON parsing — cross-check the schema-failure rate.
3. **Retry storms.** A throttled Bedrock call that retries 6 times bills every
   attempt that reached the model. Check `ExecutionTime` p95 alongside.
4. **The endpoint's standing charge.** Not per-document at all, and it usually
   dominates a low-volume run: ~$0.05/hour whether or not anything is served. If cost
   doubled and token counts did not move, ask whether an endpoint was left running.

Prices are in `config/prices.json` with the date they were retrieved. If cost looks
wrong rather than high, check that date first.

---

## 7. Tracing one document end to end

Everything is keyed on `correlation_id`, which is `bucket#key#versionId`.

```bash
CID='intake-raw-dev#incoming/doc-00042.pdf#abc123'

# The execution
aws stepfunctions list-executions --state-machine-arn "$SM_ARN" \
  --query "executions[?name=='<derived-name>']"

# Every log line for that document, across every component
for LG in /aws/vendedlogs/states/intake-intake-dev \
          /aws/lambda/intake-dev-normalize-ocr \
          /aws/lambda/intake-dev-validate \
          /aws/lambda/intake-dev-review-api; do
  echo "=== $LG ==="
  aws logs filter-log-pattern --log-group-name "$LG" \
    --filter-pattern "{ \$.correlation_id = \"$CID\" }" --max-items 20
done

# Per-stage timing
aws xray get-trace-summaries --start-time "$START" --end-time "$END" \
  --filter-expression "annotation.correlation_id = \"$CID\""
```

---

## What this runbook cannot tell you yet

Honest list, because a runbook that pretends to more coverage than it has is worse
than a short one:

- **No procedure has ever been executed.** Nothing here has been tested against a
  real incident, or against a real account at all.
- **No paging.** Every alarm publishes to one SNS topic with no subscriber. Nobody is
  on call. `alarm_email` adds an address; a rotation needs a tool this does not have.
- **No drift report** (M5). Section 3's "did the data change or did the model get
  worse" step is the one question this platform is built to answer and currently
  cannot.
- **No automated replay** for the dead-letter queue. The copy-to-self trick above
  works but is manual and does not scale past a handful of documents.
- **Timings are unmeasured.** "Wait 5 minutes for the canary" comes from the
  configured bake time, not from an observed rollback.
