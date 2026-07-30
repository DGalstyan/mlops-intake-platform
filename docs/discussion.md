# Live discussion — prepared answers

The seven questions from the assignment, answered against what this repo actually
does. Where the honest answer is "this design would not catch that", it says so —
those are the more useful answers anyway.

---

## 1. Your endpoint p99 triples with no code change and no traffic change. Walk me through the first 15 minutes.

**First: is something already handling it?**

```bash
aws sagemaker describe-endpoint --endpoint-name intake-classifier-dev --query EndpointStatus
```

`UPDATING` means a canary is shifting traffic and the latency alarm is wired into
`auto_rollback_configuration`. **Do nothing for five minutes.** Intervening during an
automatic rollback is how you end up with a half-shifted endpoint. If it already
rolled back, the incident is over and the question becomes why the variant was slow.

**Then: is it the model or the machinery?** These have different fixes.

`ModelLatency` is on the dashboard; **`OverheadLatency` is not, and that is a gap** —
it is the metric that separates "the model got slower" from "SageMaker's routing,
cold starts or readiness failures got slower", and adding it is a one-line dashboard
change I would make before the next incident. In its absence:

- `ModelLatency` up → the model itself, or the documents it is being given.
- `ModelLatency` flat but end-to-end `ExecutionTime` up → not the endpoint at all.
  Textract or
  Bedrock is throttling and the state machine is retrying with backoff, which is the
  retry policy working. Check `psi`-style token counts too: a longer document is a
  slower document all the way through.

**"No traffic change" is the claim I would test hardest.** Invocations being flat does
not mean the *work* is flat. Three things move p99 with constant request count:

1. **Documents got bigger.** `OcrCharacters` is emitted per document precisely for
   this. A scanner change or a new sender doubles the text and the model latency
   follows.
2. **Autoscaling scaled in.** Scale-in cooldown is 300s vs 60s out, deliberately, but
   a quiet period followed by a burst still gives one instance serving what two were.
   `SageMakerVariantInvocationsPerInstance` against the target of 150 shows this.
3. **A t3 instance exhausted its CPU credits.** This is the one I would suspect
   soonest and the one the current setup handles worst — `ml.t3.medium` is burstable,
   chosen for cost, and sustained load past the credit balance throttles it hard.
   That is a documented trade in the cost table, and the fix is a non-burstable
   instance type.

**What I would be missing:** per-stage latency is not a custom metric — it lives in
X-Ray, and the X-Ray annotation on `correlation_id` is **not implemented**, so the
runbook's "query the trace for this document" would return nothing. In the first 15
minutes I would be reading CloudWatch and the state machine execution history, not
traces. That is a real gap.

---

## 2. A customer says extraction quality dropped last week. Your drift metrics are all green. What now? Which of your metrics *should* have caught it, and why didn't it?

**The honest answer is that this design can genuinely miss it, and the reason is
structural rather than a threshold that needs tuning.**

Why each signal stays green:

- **Input drift** compares distributions. A sender who changed one field's *layout*
  without changing document length or vocabulary moves nothing. PSI on length and
  token count is blind to where on the page a number sits.
- **Prediction drift** compares the class mix. Extraction quality is not a class — the
  document is still correctly identified as an invoice while `total_amount` comes back
  wrong.
- **Override rate** only covers documents a human reviewed. Classification and
  extraction are *separate models*: if the classifier is confident (likely), the
  document auto-approves and no human ever sees the bad extraction.
- **Schema validation** catches *malformed* output, not *wrong* output. A hallucinated
  `total_amount` of 1284.50 is schema-valid, passes the plausibility rules, and
  auto-approves.

**What I would do, in order:**

1. Get specific documents from the customer. This is the only ground truth available.
2. Pull them from the results table by `correlation_id` and compare stored
   `extracted_fields` against the source document by hand. That answers "is it wrong,
   and which field".
3. Check `template_version` in the prompts table against `PROMPT_TEMPLATE_VERSION` in
   the code — a re-seed with a changed prompt is a silent behaviour change that
   nothing alarms on.
4. Check whether `bedrock_model_id` changed. A model swap changes extraction
   behaviour with no code diff, and the cost panel is the fastest tell: output tokens
   move when a model starts padding.
5. Check the field-level failure breakdown in the validate Lambda's logs. Even if the
   *rate* is under threshold, a single field failing on a single class is visible
   there and is not visible on the dashboard.

**Which metric should have caught it:** none of the current ones could. The two that
would:

- **Per-field null rate and extraction confidence, dimensioned by document
  class.** A field silently coming back null on 30% of one class is the actual
  signal. Today the platform tracks *validation failures*, not per-field null
  rates, so "the model returned null for `due_date`" is only visible if null
  makes the document invalid. - **An audit sample of confidently auto-approved
  documents.** Routing a small random percentage to human review anyway is the
  only mechanism that observes the auto-approved population at all. It is also
  the fix for the sampling bias in Q7 — one change addresses both, which is why
  it is the highest-value thing missing.

---

## 3. Bedrock deprecates the model version you pinned, with 30 days' notice. What has to change, and how much of it is automated?

**What changes: one tfvars line.**

```hcl
bedrock_model_id = "anthropic.claude-3-5-haiku-20241022-v1:0"  # -> the successor
```

That variable flows to three places automatically: the `Extract` state's `ModelId`,
the IAM policy's `Resource` (scoped to the specific foundation-model ARN, so the
permission re-scopes itself), and the cost math.

**Why it is only one line** — three deliberate decisions, each of which could have
made this a rewrite:

1. **The prompt is data, not code.** It lives in DynamoDB, rendered from
   `schemas/*.json`. Changing models does not touch the prompt, and if the new model
   needs a different prompt shape that is a `make seed-prompts` away with no deploy.
2. **The response parser tolerates three shapes.** `parse_model_json` handles the
   Messages API, the older completion shape, and a bare object, plus markdown fences
   and surrounding prose. A model that formats differently does not break parsing.
3. **The IAM policy names the model variable, not a wildcard.** No `bedrock:*` to
   forget to narrow later.

**Where that one-line claim breaks, and it does:** the successor to a pinned Claude
model is generally **not invocable via a bare `foundation-model/<id>` ARN**. Current
models are served through cross-region **inference profiles**, which means:

- a different ARN shape (`application-inference-profile/` or `inference-profile/`),
- `bedrock:InvokeModel` on **both** the profile ARN *and* the underlying
  `foundation-model` ARNs in **every region the profile spans**, not just ours,
- so `infra/modules/intake/statemachine.tf`'s single-ARN statement becomes a
  multi-ARN, multi-region policy.

That is a policy restructure, not a tfvars edit — perhaps two hours rather than two
minutes, and it is not hypothetical: the pinned `claude-3-5-haiku` is already a
legacy model. The parts that genuinely *are* one line are the model id itself and the
prompt (which needs no change at all); the IAM is not.

**What is NOT automated, and should not be:**

- **The prices.** `config/prices.json` is per-model, and a swap without updating it
  makes the cost panel *wrong but plausible* — worse than blank, because nobody
  investigates a reasonable-looking number. There is a test asserting the priced model
  matches the Terraform default, so this fails CI rather than drifting silently.
- **Extraction quality.** The gate compares *classifier* versions on the golden set;
  there is no equivalent offline eval for the extraction model. Swapping it is
  currently an unvalidated change, and that is the biggest gap this question exposes.
  With 30 days I would build a small extraction eval set — 50 documents with
  hand-checked fields — and diff old model against new before switching.

**Rollback:** a tfvars revert plus an apply, with no image rebuild, because the model
id is not baked into anything. Note the apply itself is the constraint — the CI deploy
role's permissions have never been exercised, so the first rollback would be a human
running `make apply`.

---

## 4. Why did you gate registry approval on a human? When would you remove the human, and what evidence would you need?

**Why the human is there.** The gate proves a candidate is better *on the golden set*.
It cannot prove three things that matter more:

1. **That the golden set still represents production.** It is synthetic and frozen at
   M1. Every week it ages.
2. **That the training data was not poisoned by its own collection process.** The
   corrections come from human review, which only sees low-confidence documents (Q7).
   A candidate can score better on the golden set while having over-fitted to the hard
   slice.
3. **That the per-class picture is acceptable.** The gate enforces a floor, but a floor
   is not a judgement — a model that drops `id_document` from 0.95 to 0.62 passes a
   0.60 floor and should still probably not ship.

The human is the only place those get looked at. That is why the approval notification
tells the reviewer to check per-class F1 and points at the sampling-bias section,
rather than just saying "a candidate is ready".

**What I would need to remove them:**

1. **An audited random sample of production traffic** to evaluate against, replacing
   the frozen synthetic golden set. Without this, every automated decision is made on
   data that is not what the model sees.
2. **A demonstrated auto-rollback.** The canary and the alarms exist; nothing has ever
   rolled back. Removing a human gate while the automated safety net is unproven
   swaps a control that works for one that is asserted.
3. **A track record.** Ten to twenty retrain cycles where the gate's verdict and the
   human's decision agreed. If they always agree, the human is ceremony. If they
   sometimes disagree, that disagreement is exactly the signal that must be encoded
   before automating.
4. **Extraction-side evaluation** (see Q3), so "the model got better" covers both
   models rather than only the classifier.

**What I would keep even then:** the human on *staging → production*. Automating
dev-to-staging with a proven rollback is a reasonable trade; automating the last hop
removes the last place anyone looks at what is about to serve customers.

---

## 5. You need to support 40 document types instead of 4, added by a customer success team, not engineers. What survives your current design and what gets rewritten?

**What survives, and why:**

- **The schemas.** `schemas/*.json` is the source of truth, and `render_all()` is
  driven by *what is on disk* rather than a hardcoded list. Dropping in
  `purchase_order.json` gives you a rendered prompt, a response schema, and a
  validator with no code change.
- **The extraction path.** The prompt is fetched from DynamoDB by class name, so a new
  class needs a `make seed-prompts`, not a deploy.
- **All the infrastructure.** One KMS key, one bucket set, one endpoint, one state
  machine, regardless of class count.
- **The intake state machine.** No per-class branching exists — routing is confidence
  plus one always-review rule.

**What breaks at 40, honestly:**

1. **`DOCUMENT_CLASSES` is a frozen tuple in `src/config.py`,** and its *order* defines
   the column order of every confusion matrix and per-class array in stored artifacts.
   Adding a class is an append; the comment says so. But a customer success team cannot
   append to a Python constant — that is the crux of this question.
2. **`FIELD_RULES` requires an explicit entry per class**, deliberately, so "no rules"
   is a decision rather than an oversight. At 40 classes that becomes 40 entries a
   non-engineer cannot write.
3. **The always-review rule is a hardcoded class name** in two Choice states.
4. **The classifier.** TF-IDF over 40 classes with human-authored examples will be far
   worse than over 4, and the confidence threshold that gives 12% review at 4 classes
   will give something very different at 40.
5. **Per-class dashboard widgets.** Four series is legible; forty is a smear.
6. **Cost.** Per-class metrics are dimensioned by `DocumentClass`, so custom-metric
   count scales with class count — 9 metrics × 40 classes is a real bill.

**What I would change:** make the class list **data**, loaded from `schemas/` at
startup rather than declared in code, with `FIELD_RULES` and the always-review flag
moving *into* each schema file as extension keys. Then a new class is genuinely one
file. The dashboard moves to top-N-by-volume plus an "other" aggregate. The
always-review flag becomes a property in the schema, read by the state machine from
the same DynamoDB item as the prompt.

That is a day of work and no architectural change, which is the useful answer
here. The part that *is* architectural is the classifier: 40 classes wants a
different model and probably a hierarchical routing step, and no amount of
configuration fixes that.

---

## 6. Cost has doubled. Show me where you'd look, in order.

Cheapest checks first, ordered by how likely each is to be the answer.

**1. Is an endpoint running that should not be?** (~30 seconds)
```bash
aws sagemaker list-endpoints --query 'Endpoints[].{n:EndpointName,s:EndpointStatus}'
```
`ml.t3.medium` bills ~$0.05/hour whether or not it serves a single request — ~$36/month
for an idle endpoint. In a low-volume system this dominates everything else, which is
why `deploy_endpoint` defaults to false. If cost doubled and token counts did not
move, this is almost always it.

**2. Input tokens per document** (dashboard, cost section). A jump means the OCR text
grew: noisier scans, bigger documents, or a new sender. Bedrock is per-token, so
document size *is* cost.

**3. Output tokens per document.** A jump usually means the model started
explaining itself rather than returning bare JSON, which also breaks parsing, so
cross-check the schema-failure rate. These two moving together is a
model-behaviour change, not a volume change.

**4. Retry storms.** A throttled Bedrock call that retries six times bills every
attempt that reached the model. `ExecutionTime` p95 rising alongside cost points here.
The retry policy is doing its job; the cost is the price of not losing documents.

**5. Data capture volume.** 100% sampling in dev is deliberate — M5 needs a complete
picture — but it is a per-request S3 write plus storage plus the drift job's scan. In
an environment with real traffic this is the first thing to sample down.

**6. CloudWatch custom metrics.** Charged per metric-name × dimension-value
combination. Adding a dimension multiplies the bill. `correlation_id` is deliberately
never a dimension for exactly this reason — it would create one custom metric per
document. If someone added a high-cardinality dimension, this is where it shows.

**7. KMS.** ~$1/key/month plus per-request charges, and a key survives `make destroy`
for 7 days in `PendingDeletion` still billing.

**What I would fix first if this were real:** the cost dashboard covers Bedrock only.
Textract and the endpoint's standing charge are named in the panel text but not
graphed, and they are the two most likely culprits. That is the gap this question
exposes.

---

## 7. Where's the sampling bias in your retraining data, and what does it do to your model after three retrain cycles?

**Where it is:** retraining data comes from human corrections. Humans only
review documents that were **low-confidence** or in an **always-review class**.
So the labelled set is a sample of exactly the documents the model already finds
hard, and it is selected *by the model's own confidence*, which is the worst
possible selector because it correlates directly with the thing being learned.

**Three cycles, concretely:**

- **Cycle 1.** Train on training data plus corrections. The corrections are all hard
  cases, so the decision boundary shifts toward them. Golden-set macro-F1 may improve —
  the gate passes.
- **Cycle 2.** The shifted boundary means more documents now fall below the confidence
  threshold, so more get reviewed, so cycle 2's correction set is *larger and more
  biased* than cycle 1's. The model gets better at ambiguity and no better at the
  confident majority, because that majority never enters the training set.
- **Cycle 3.** The model is now specialised for the hard slice. Its errors on the easy
  majority are unchanged and invisible. **And the override rate — the primary quality
  proxy — can be falling the whole time**, because the documents reaching reviewers are
  increasingly ones the model now handles well.

**That is the failure mode: the quality metric improves while real accuracy degrades,
and no metric in this platform would catch it.** Confidently-wrong documents
auto-approve, are never reviewed, are never corrected, and never enter the loop. The
model's specific blind spot is the one region the feedback loop structurally cannot
reach.

**What I would do, in priority order:**

1. **Audit sampling.** Route ~1–2% of *confidently auto-approved* documents to human
   review anyway. This is the only mechanism that puts confidently-wrong documents into
   the training data, and it simultaneously provides an unbiased accuracy estimate.
   Fixed, predictable cost. **Not implemented — the single highest-value addition to
   this design**, and the same change that answers Q2.
2. **Stratified sampling** when assembling the retrain set, so its confidence
   distribution matches production rather than the review queue's.
3. **Importance weighting.** The selection probability is *known* — it is a threshold
   on a recorded confidence — so the correction is computable rather than estimated.
4. **A separate audited hold-out**, never sourced from review, to evaluate against. The
   golden set plays this role today but is synthetic and frozen.

**What the platform does do:** the corrections table records `original_predicted_class`,
`original_confidence` and `was_prediction_correct` on every row, specifically so the
bias is *measurable* — you can plot the confidence distribution of the labelled set
against production and see the gap. Measuring it is not fixing it, and I would not
claim otherwise.
