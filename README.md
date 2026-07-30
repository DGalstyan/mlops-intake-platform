# Document-Intake MLOps Platform

A document-intake pipeline built around a swappable classifier: documents land in
S3, get OCR'd, classified, routed by confidence, field-extracted against a
per-class JSON schema, validated, and then either auto-approved or parked for
human review. Corrections feed back as labelled training data. Drift detection
watches production traffic against a training-time baseline and can trigger a
gated retrain that a human must approve before it canary-deploys.

**Model accuracy is an explicit non-goal here.** The classifier is deliberately
simple. What this repo is about is the platform around it: reproducible
training, immutable artifacts, gated releases with automatic rollback,
observability that measures business outcomes rather than CPU, and honest drift
detection.

---

## Build status

This is the honest current state. **No AWS resource has ever been created from this
repo** — there are no credentials on the machine it was built on. All seven
milestones have code; none of it has been deployed. What that means per milestone is
in the table below, and what is *unverified* about each is in Known gaps.

| Milestone | Status | What exists |
|---|---|---|
| **M0** Foundations (IaC) | **Code complete, never applied** | `infra/` — state backend, KMS, ECR, 4 buckets, per-component IAM. `terraform validate` + `fmt` clean on all three roots. |
| **M1** Training + Registry | **Code complete, runs locally; never run on SageMaker** | Generator + 4 schemas, swappable model interface, train/evaluate entrypoints, calibration + ECE, baseline artifact, registry assembly. Tested and type-checked locally. |
| **M2** Deployment | **Code complete; endpoint never deployed, image never built** | Inference handlers + serving layer with verified `/ping`/`/invocations` contract, Dockerfile, endpoint Terraform with canary + alarm-driven auto-rollback, data capture, measured autoscaling target, approved-only resolver, post-deploy smoke test. |
| **M3** Orchestration + HITL | **Code + Terraform complete, never deployed** | 30-state intake ASL with retry/jitter/catch throughout, idempotency ledger, `.waitForTaskToken` review, corrections as labelled data, dead-letter path. 5 DynamoDB tables, 3 Lambdas each with its own role, EventBridge trigger (S3 notifications enabled on the bucket), review API. Local simulation produces the two required traces. |
| **M4** Observability | **Code + Terraform complete, never deployed** | 11 custom metrics emitted via direct SDK, 10 alarms, 4-section dashboard in Terraform, prices as shared data, generated alarm inventory, runbook. No dashboard screenshot — needs a deployment. |
| **M5** Drift + Retraining | **Drift detection working with real evidence; retrain SM written, never run; no Terraform** | PSI/KS/categorical drift math (tested against hand-computed values), three-family classification, drift reports from the real baseline and shifted batch. Retrain state machine with the gate and no deploy path. |
| **M6** CI/CD | **GREEN PR + main runs on GitHub Actions** | PR: lint, mypy --strict, 346 tests, 5 regression proofs, terraform validate, container build + contract check. main: verify, then AWS jobs gated on a deploy role. Retrain workflow is `workflow_dispatch` only. |

**What "never applied" means:** `terraform plan` has not been run against a real
account, because no AWS credentials are configured. So `fmt` and `validate` are
verified; `plan`, `apply` and `destroy` are **not**. Treat the cost table below as
calculated-from-price-lists, not observed-on-a-bill.

**What is in `evidence/` is therefore local, and labelled as such.** `m1/` holds real
training and evaluation output. `m2/` holds a real load measurement but **no rollback
recording** — M2's actual deliverable. `m3/` holds traces from a local *simulation*,
not a Step Functions execution. Each evidence folder states its own caveat; none of
them are a substitute for a run against AWS.

**Prepared answers to the seven live-discussion questions** are in
[`docs/discussion.md`](./docs/discussion.md), including the two where the honest
answer is "this design would not catch that". The evidence index — what exists, what
is a local substitute, and what is absent — is [`evidence/README.md`](./evidence/README.md).

The per-milestone plan of record is in [`tasks/`](./tasks/), with an audit of M0
against the grading rubric recorded in
[`tasks/M0-foundations.md`](./tasks/M0-foundations.md).

---

## Architecture

Status key: `[x]` written and tested · `[~]` partially written · `[ ]` not started.
Nothing here has been deployed.

```
[~] S3 upload ──► EventBridge ──► Step Functions "intake" state machine
    (ASL, handlers and Terraform written and tested; never deployed)
                                       │
                                       ├─ 1. OCR            (Textract)
                                       ├─ 2. Classify       (SageMaker endpoint)
                                       ├─ 3. Route          (confidence + business rule)
                                       ├─ 4. Extract        (Bedrock, class prompt + JSON schema)
                                       ├─ 5. Validate       (schema + field rules)
                                       └─ 6a. Auto-approve  ──► results store
                                           6b. Human review ──► review queue ──► corrections
                                                                                      │
                         ┌────────────────────────────────────────────────────────────┘
                         ▼
[~]     Step Functions "retrain" state machine  (ASL written; no Terraform)
          train ──► evaluate vs champion ──► gate ──► Model Registry (PendingManualApproval)
                                                              │ human approval event
                                                              ▼
                                              canary deploy ──► auto-rollback on alarm

[x] Foundations, and the inference endpoint (infra/, Terraform written):
      KMS key (per env) · ECR repo (immutable tags) · 4 S3 buckets
      (raw / processed / artifacts / data-capture) · per-component IAM roles
      · S3 remote state with native locking · GitHub Actions OIDC provider
      · SageMaker endpoint: data capture, autoscaling, canary + auto-rollback
```

Four document classes — `invoice`, `medical_report`, `id_document`,
`correspondence` — each with its own extraction schema. Schemas live in
`schemas/` as data, not code, so adding a class does not mean editing Terraform,
ASL, or handlers.

### Repo layout

```
infra/           Terraform. bootstrap/ (state backend) + modules/ + envs/{dev,staging}/
src/             data generator, training, inference, pipeline handlers, drift math
schemas/         one JSON Schema per document class (source of truth)
statemachines/   ASL definitions (intake, retrain)
tests/           unit + contract + ASL-validation tests
evidence/        dashboard shot, rollback proof, drift report, traces, CI runs
docs/            decisions.md (decision log), runbook.md, ASSIGNMENT.md
tasks/           milestone breakdown M0-M7
```

---

## Quickstart from an empty AWS account

**Prerequisites**

- Terraform **>= 1.10** — the S3 backend uses native locking (`use_lockfile`),
  which does not exist before 1.10. `terraform init` fails outright on older
  versions rather than degrading.
- AWS CLI v2, with credentials for an account you don't mind creating resources
  in. `aws sts get-caller-identity` must work — the Makefile derives the state
  bucket name from your account id.
- No other manual step. No console clicks.

### The model side (no AWS needed)

Everything in M1 runs locally, which is deliberate — the training and evaluation
code is a plain Python package that SageMaker happens to invoke, not something
that only works inside a job.

```bash
make venv            # .venv with pinned dependencies (Python 3.12)
make test            # 346 tests, mypy-strict clean
make typecheck       # mypy --strict, clean
make data            # deterministic corpus + content-addressed snapshot id
make two-versions    # the M1 deliverable: two distinguishable registry versions
make measure-throughput  # the load numbers behind the M2 autoscaling target
make simulate-intake     # run the intake flow locally, regenerate the M3 traces
make prompts             # show the extraction prompts rendered from schemas/
```

`make two-versions` reproduces the numbers in `evidence/m1/two-versions.md`
exactly, from the seed in `src/config.py`.

### Infrastructure

```bash
# 1. Create the remote state backend. Local state, run once.
#    If your account already federates GitHub Actions, add
#    -var="create_github_oidc_provider=false" — the provider is an account
#    singleton and will otherwise fail with EntityAlreadyExists.
make bootstrap

# 2. See what would be created, then create it.
make plan  ENV=dev
make apply ENV=dev

# 3. Static checks (need no credentials — this is what CI runs on a PR).
make fmt-check
make validate-all
```

`ENV` accepts `dev` or `staging`; anything else is rejected by the Makefile.
Per-environment values live in `infra/envs/<env>/<env>.tfvars`.

**Before you run this,** set `github_repository` in both tfvars files to your own
`org/repo`. It scopes the OIDC trust condition on the CI deploy role, so a wrong
value means CI cannot assume the role. It fails closed, not open.

### Teardown

Order matters.

```bash
make destroy ENV=dev
make destroy ENV=staging      # if you applied it
make destroy-bootstrap        # LAST — the state backend itself
```

**Two things deliberately survive `make destroy`:**

1. **The KMS key**, for 7 days. AWS enforces a 7–30 day `PendingDeletion`
   window and zero is not permitted, so 7 is the floor. It keeps billing
   (~$0.23 prorated per environment) and its alias is deleted immediately, so it
   shows up in the console without a friendly name. Nothing can shorten this.
2. **The state bucket and the OIDC provider**, until `make destroy-bootstrap` —
   they are shared across environments, so tearing them down with the first
   environment would strand the second.

Everything else is gone. All four data buckets are `force_destroy` and the ECR
repository is `force_delete`, specifically so a non-empty bucket or a repo full
of images cannot block its own deletion. Verify with:

```bash
aws resourcegroupstaggingapi get-resources --tag-filters Key=project,Values=intake
```

---

## The model, its metrics, and the baseline artifact

### Accuracy is a non-goal, so here is what the numbers are for

The classifier is TF-IDF + multinomial logistic regression, probability-calibrated
with cross-validated isotonic regression. On the frozen 240-document golden set it
scores **macro-F1 0.9417, ECE 0.0140**.

The synthetic generator is deliberately tuned so this is *not* higher. An earlier
version put the class name in each document's header; the model read the label
straight off the text and scored macro-F1 1.00 with ECE 0.0003 and every
confidence pinned at 1.0. That looks like success and is worthless — with a
perfect classifier, calibration has nothing to measure, the Route state's
confidence threshold can never fire, the human-review queue stays empty, and the
drift demo has no headroom to move. A too-easy dataset silently guts M3, M4 and
M5. The term-mixture weights in `src/data/generate.py` were swept against
held-out macro-F1 and the fraction of documents falling below the auto-approve
threshold; the chosen point puts ~12% of documents into human review.

### Which metrics measure model quality vs. system health

`metrics.json` is written twice, and the difference matters:

- `train.py` writes it with `"split": "train"`, `"is_held_out": false`. These are
  convergence-debugging numbers. They are *not* a quality claim.
- `evaluate.py` writes it with `"split": "golden"`, `"is_held_out": true`. These
  gate releases and feed the retrain comparison.

`register.py` **refuses** to attach anything that is not the golden variant, so
the labelling is load-bearing rather than advisory. The full model-quality vs
system-health discussion is in the Observability section below.

### Calibration is a correctness property here, not a tuning detail

The two registry versions make the point better than prose: v2 (calibration
disabled) is **more accurate** than v1 — macro-F1 0.9543 vs 0.9417 — while its
ECE is **19× worse**, 0.2622 vs 0.0140. Since the Route state gates auto-approval
on `max(predict_proba)`, v2 would confidently auto-approve documents it should
have escalated, while winning on every accuracy-shaped metric. Choosing v2 on
macro-F1 alone is exactly the mistake this pair exists to expose.

### What is in the baseline statistics artifact, and why

`baseline_statistics.json` is the reference the M5 drift job compares production
traffic against. It is versioned (`schema_version`), and `load_baseline()` refuses
a major version it does not recognise — a drift job that silently reads a shape it
does not understand reports numbers computed against the wrong fields, which is
worse than not running at all.

| Contents | Drift question it answers |
|---|---|
| Per-class prediction priors | **Prediction drift.** The only signal available with no ground truth, which is the normal production case. |
| Document char-length + token-count distributions, with **fixed histogram edges** | **Input drift.** Needs no model, so it still works when the endpoint is down. |
| Confidence histogram | **Concept-drift proxy.** Confidence decaying while inputs and predictions look stable means the world changed in a way the features do not capture. |
| Per-feature TF-IDF means and variances (top 200) | Lets drift be **attributed** to specific vocabulary rather than reported as "something moved". |
| Vocabulary size | A TF-IDF model silently ignores unseen tokens, so falling coverage means it is going blind to its input while confidence stays high. Nothing else reveals that. |

Two deliberate omissions:

- **Histogram edges travel with the artifact.** Recomputing bins from live data
  would compare two differently-binned distributions and manufacture drift out of
  nothing.
- **No accuracy or F1.** Those are properties of a model scored against labels
  and live in `metrics.json`. Mixing them in here invites the precise mistake M5
  must avoid: treating "the data changed" and "the model got worse" as one
  signal.

### Lineage

Every registered version carries the data snapshot id (a **content hash** of the
exact training bytes, not a UUID — so an identical id proves identical input), the
git SHA, the training image digest, and the resolved dependency versions. Missing
values are recorded as the explicit string `unknown` rather than defaulted,
because only an explicit unknown is detectable in review. Locally the image digest
is legitimately unknown and training prints a warning saying so.

## Drift, retraining, and the sampling bias

### Separating "the data changed" from "the model got worse"

Three signal families, kept deliberately separate, because **the correct response
differs and one of the responses is actively harmful applied to the other**:

| Family | Answers | Needs the model? |
|---|---|---|
| **Input** — PSI + median shift on document length and token count | has the incoming data changed? | no — works when the endpoint is down |
| **Prediction** — categorical PSI on the predicted-class mix | has the output mix changed? | endpoint output only |
| **Concept** — override-rate trend, confidence p10 decay | has the *relationship* changed? | yes, plus human feedback |

Four verdicts, and only two of them retrain:

- `NO_DRIFT` → no action.
- `DATA_CHANGED` → **do not retrain.** Inputs moved, the model is coping.
- `MODEL_DECAYED` → retrain candidate. Inputs steady, the model is getting the
  reviewed slice wrong more often. Invisible to input-drift tests.
- `DATA_CHANGED_AND_MODEL_DECAYED` → retrain, highest priority.

**Why conflating them is a bug:** retraining in response to input drift alone spends
money fitting the new distribution's noise, and it does so using labels sourced from
human review — which only covers the low-confidence slice. You can make the model
*worse* by responding to a signal that did not require a response. A single combined
"drift score" maps these opposite situations onto the same number and prescribes the
same action for both.

`evidence/m5/` demonstrates this with real numbers: the deliberately shifted batch
produces PSI **19.8** on document length — roughly 79× the significant-shift
threshold — while the class mix is unchanged (0.0001) and confidence *rises* 35%. The
verdict is `DATA_CHANGED` and the retrain trigger stays off.

### The retrain gate, and why a human is in it

`train → evaluate → gate → register(PendingManualApproval) → notify`. The state
machine **never deploys**. It stops at registration; a human approving that registry
version is what emits the EventBridge event that starts the M2 canary deploy. There
is a test asserting no endpoint API appears anywhere in the definition, and another
asserting the approval status is hardcoded rather than taken from input — if it were
an input, a caller could pass `Approved` and self-deploy.

The gate is two independent conditions, both computed by the *same* `evaluate.py`
that produced the champion's numbers:

1. macro-F1 must beat the champion by ≥ `GATE_MIN_MACRO_F1_IMPROVEMENT` (0.02). A
   margin, not `>`, because on a 240-document golden set the difference between two
   runs is often noise, and a gate that fires on noise gets ignored.
2. no single class may fall below `GATE_MIN_PER_CLASS_F1` (0.60). An overall gain can
   hide one collapsed class — a model that improved on average while becoming useless
   for `id_document` must not ship.

**Why a human, and when I would remove them.** The gate proves a candidate is better
*on the golden set*. It cannot prove the candidate is better on the traffic that has
been arriving since the golden set was frozen, and it cannot see the sampling bias
below. I would remove the human when there is (a) an audited random sample of
production documents to evaluate against, not just a frozen synthetic set, and (b)
enough deploy history that the canary + auto-rollback has demonstrably caught a bad
version. Until both hold, the human is the only thing standing between "the numbers
improved" and "it is serving traffic".

### The sampling bias — the trap, stated plainly

**The retraining data comes from human review. Human review only sees
low-confidence and always-review documents. Therefore the labelled data is a biased
sample of exactly the documents the model already finds hard.**

Three consequences, and the third is the one that matters:

1. **The labelled set over-represents ambiguity.** Train naively on it and the model
   optimises for the hard slice at the expense of the easy majority it was already
   getting right.
2. **Confidently-wrong documents never enter the loop.** They auto-approve, so nobody
   reviews them, so they are never corrected, so they are never in the training data.
   The model's *specific* blind spot is the one region the feedback loop structurally
   cannot reach.
3. **It compounds.** After three retrain cycles: cycle 1 shifts the decision boundary
   toward the hard slice; more documents fall below the confidence threshold and are
   reviewed, so cycle 2's training data is *more* biased than cycle 1's; the model
   becomes progressively better at ambiguous documents and progressively more
   confident about a shrinking region it never gets corrected on. **The override rate
   can fall the whole time**, because the documents being reviewed are the ones the
   model now handles well — so the primary quality proxy improves while real accuracy
   degrades. That is the failure mode, and no metric currently in this platform would
   catch it.

**What I would do about it,** in the order I would do it:

1. **Audit sampling — the fix that matters.** Route a small random percentage of
   *confidently auto-approved* documents to human review anyway. This is the only
   mechanism that puts confidently-wrong documents into the feedback loop, and it
   simultaneously gives an unbiased accuracy estimate. Cost is a fixed small tax on
   review capacity. **Not implemented**, and it is the single highest-value addition
   to this design.
2. **Stratified sampling across confidence bins** when assembling training data, so
   the retrain set's confidence distribution matches production rather than the review
   queue's.
3. **Importance weighting** to correct the selection probability, if the routing rule
   is known — it is, since it is a threshold on a recorded confidence.
4. **Keep an audited hold-out set entirely separate** from review-sourced data, and
   evaluate on it. The golden set serves this role today, but it is synthetic and
   frozen at M1, so it ages.

The corrections table records `original_predicted_class`, `original_confidence` and
`was_prediction_correct` on every row precisely so this bias is *measurable* — you can
see the confidence distribution of the labelled set and compare it to production.
Measuring it is not fixing it.

## Observability: what actually measures model quality

The assignment asks this directly, so here is the direct answer.

### There is no ground truth in production

Every document that auto-approves does so *because nobody checked it*. So accuracy
is not measurable in production — only on the frozen golden set, offline, at M1.
Everything below is a **proxy**, and the useful question about a proxy is not "what
does it say" but "what is it blind to".

### The split

| Metric | Measures | Blind to |
|---|---|---|
| `HumanOverrideRate` | **model quality — primary proxy** | Confidently-wrong documents. Reviewers only see the low-confidence and always-review slice. |
| `Confidence` p10 / p50 | **model quality — concept-drift proxy** | A model that is confidently wrong in a *new* way. Calibration is measured offline; a miscalibrated model reports high confidence while being wrong. |
| `SchemaValidationFailureRate` | **model quality — extraction** | Extraction that is plausible but wrong. A hallucinated invoice number passes every schema check. |
| `AutoApprovalRate` | **model quality — indirect** | Cannot distinguish "model got worse" from "harder documents arrived". Needs the drift report to disambiguate. |
| `EndToEndLatencyP95`, per-stage latency | system health | Everything about correctness. |
| `ExecutionsFailed`, DLQ depth | system health / data safety | Everything about correctness. A perfectly-running pipeline emitting wrong answers is green here. |
| `EstimatedCostPerDocument`, token volumes | cost | Correctness, except indirectly — rising output tokens often means the model started padding, which also breaks JSON parsing. |

The whole reason the model-health section exists is that **every system-health metric
can be green while the model is quietly wrong.** That is the normal failure, not the
exotic one.

### The primary proxy, and where it misleads

**`HumanOverrideRate` on the reviewed slice.** When reviewers change the class more
often than they used to, something moved.

It misleads in three specific ways, and all three matter:

1. **Selection bias by construction.** The denominator is documents that were routed
   to a human — low confidence, or an always-review class. That slice is *selected
   for being hard*, so the absolute rate is high and meaningless. Only the **change**
   is informative. This is the same bias that poisons the retraining data, and it is
   the subject of M5's sampling-bias section.
2. **It cannot see the failure that matters most.** A confidently-wrong document
   auto-approves, so no human ever looks at it, so it never enters this metric. If
   the model becomes confidently wrong — which is exactly what a miscalibrated model
   does — the override rate can *fall* while quality collapses.
3. **It measures reviewers as much as the model.** A new reviewer who disagrees about
   ambiguous documents moves this number with no model change at all. The corrections
   table records `reviewer_id` precisely so that can be checked.

### The scenario this design would fail

*"A customer says extraction quality dropped last week. Your drift metrics are all
green."*

The honest answer: **the current metric set can miss this**, and the reason is
structural rather than a tuning problem.

- Input drift compares distributions. If a sender changed one field's *layout*
  without changing document length or vocabulary, the distributions barely move.
- Prediction drift compares class balance. Extraction quality is not a class.
- The override rate only covers reviewed documents. If the affected documents are
  confidently classified — likely, since classification and extraction are separate
  models — they auto-approve and are never reviewed.
- Schema validation catches *malformed* output, not *wrong* output. A confidently
  hallucinated `total_amount` is schema-valid.

What *should* have caught it, and what would close the gap:

- **Field-level extraction confidence and null rates per field.** A field that
  silently starts coming back null on 30% of one class is the signal. Not
  implemented — the metric set tracks validation failures, not field-level null
  rates.
- **An audit sample of auto-approved documents.** Routing a small random percentage
  of *confidently* auto-approved documents to human review anyway is the only way to
  measure the blind spot, and it is also the fix for the retraining sampling bias.
  Not implemented, and it is the single highest-value addition to this design.

Both are in the known-gaps list rather than quietly absent.

### Why rates are metric math over raw counters

The state machine emits counters — `DocumentsProcessed`, `AutoApproved`,
`HumanOverride`, `LLMInputTokens` — and every rate and the cost figure are derived
with CloudWatch metric math. Two reasons:

1. A pre-averaged rate is frozen at the period it was computed for. Counters let the
   dashboard show a 15-minute rate while an alarm evaluates an hourly one over the
   same data.
2. `EstimatedCostPerDocument` has to be computed from *real token counts × documented
   prices*. Emitting a pre-computed cost would bake today's price list into stored
   datapoints and make last week's cost unrecomputable when prices change. Prices
   live in `config/prices.json`, read by both Terraform and Python, with the date they
   were retrieved.

`correlation_id` is deliberately **not** a metric dimension. CloudWatch bills per
metric-name × dimension-value combination, so dimensioning by it would create one
custom metric per document. It belongs in logs and traces, and there is a test
asserting it never becomes a dimension.

### Alarms

10 alarms, inventoried in [`evidence/m4/alarm-inventory.md`](./evidence/m4/alarm-inventory.md)
— generated from the Terraform by `make alarm-inventory`, with a test asserting the
committed file matches a fresh render. Each carries what breaks, the first response,
and a runbook link in its own description, because an alarm that fires at 3am without
saying what to do has failed at the only moment it matters.

**Who is paged: nobody, yet.** Every alarm publishes to one SNS topic with no
subscriber by default. `alarm_email` adds an address, but a real rotation needs an
on-call tool and an escalation policy. Inventing a paging story this repo does not
implement would be worse than saying so. The split that *would* matter: the
model-quality alarms are not wake-someone-up events — they need a human with the
corrections table and a day to think — while the pipeline-health and dead-letter
alarms are.

## Cost

Prices are us-east-1 list, October 2025, and are the constants used below.
**These are calculated, not observed** — see Build status.

| Service | Price constant | M0 standing cost |
|---|---|---|
| KMS customer-managed key | $1.00 / key / month | $1.00 per environment |
| KMS API requests | $0.03 / 10k requests | negligible at M0 |
| S3 Standard storage | $0.023 / GB / month | ~$0 (buckets are empty) |
| S3 PUT / LIST | $0.005 / 1k requests | ~$0 |
| S3 GET | $0.0004 / 1k requests | ~$0 |
| ECR storage | $0.10 / GB / month | ~$0 (no image pushed yet) |

**M0 applied to `dev` only, torn down same day: well under $1.** The only
meaningful line is the KMS key, and even that is prorated hourly plus its 7-day
deletion window — call it $0.25.

Costs that arrive with later milestones, so you know where the ~$15 budget
actually goes:

| Milestone | Driver | Notes |
|---|---|---|
| M1 | SageMaker Training + Processing jobs | Billed per second of instance time. Minutes per run on a small instance. |
| M2 | SageMaker inference endpoint | **The dominant cost, and now decided.** Real-time `ml.t3.medium` at ~$0.05/hr — ~$1.20/day if left running, and briefly double that during a canary while both fleets are alive. Serverless was rejected: it cannot do data capture, autoscaling, or canary rollback, which are the inputs to M5 and the whole of M2. `deploy_endpoint` defaults to **false**, `max_capacity = 2` caps scale-out, and `make destroy` removes it. |
| M3 | Textract, Bedrock | Textract `DetectDocumentText` ~$1.50/1k pages. Bedrock is per-token and model-dependent; the constant gets pinned when the model is chosen. |
| M4 | CloudWatch custom metrics, dashboards | Per-metric monthly + dashboard fee. Small but not free at 11 custom metrics x 2 dimensions. |
| M5 | Scheduled drift Processing job | Per-second instance time, once per schedule tick. |

The single biggest lever on total spend is **not leaving an endpoint running**.
That is what `make destroy` is for.

---

## Decisions

The full decision log is [`docs/decisions.md`](./docs/decisions.md) — each entry
in the form "chose X over Y because Z, and here's when I'd flip it", including
the alternatives rejected and one piece of my own over-engineering that I
deleted.

The ones worth knowing before reading the code:

- **S3 native locking, not a DynamoDB lock table.** One resource instead of two,
  which matters for a clean teardown. Requires Terraform >= 1.10 and gives up
  DynamoDB's more discoverable stuck-lock debugging.
- **One KMS key per environment**, not per bucket (four near-identical policies
  for no isolation gain) and not one shared across environments (blurs the
  dev/staging blast radius).
- **The state bucket name is derived from `(project, account)`, never read from
  Terraform output.** The bootstrap root keeps local, gitignored state, so a
  lookup only works on the machine that ran `make bootstrap` — it fails on every
  CI runner. The cost is a naming contract duplicated across three files.
- **The GitHub OIDC provider's creation is optional** because it is an account
  singleton, and the environment roots resolve it with a `data` source so they
  don't care who created it.
- **`force_destroy` everywhere, `prevent_destroy` nowhere.** Deliberate for a
  graded, budget-capped run with synthetic data; exactly wrong for production,
  and the log says so.

### On `Resource: "*"`

Every occurrence falls into one of four categories, all AWS restrictions rather than
scoping choices: **KMS key-policy self-reference** (a key's ARN cannot appear inside
its own policy), **`ecr:GetAuthorizationToken`**, the **CloudWatch Logs delivery
API**, and **X-Ray segment submission** — none of which support resource-level
permissions. There is also one `textract:DetectDocumentText`, where the control that
matters is the separately-scoped `s3:GetObject` on the raw bucket: Textract can only
read what the role can read.

`docs/decisions.md` carries the per-category inventory with the AWS restriction
behind each, and `make wildcard-audit` regenerates the file:line list. Deliberately a
command rather than a count in prose — an earlier revision hardcoded "six" and it
went stale as soon as M3 landed.

There is additionally one `Principal: "*"` / `Action: "s3:*"` in the bootstrap root.
That one is a **Deny** rejecting non-TLS requests to the state bucket, which is the
point.

There is no `iam:*`, no `Action: "*"` grant, and no service-level action
wildcard anywhere.

---

## Where this landed

Six milestones of code, none of it deployed. The split is worth stating plainly
because it is the whole character of this submission:

**Verified, by something other than my own assertion**
- 346 tests, `mypy --strict` on 35 files, `ruff` clean, `terraform validate` on three roots
- A **green PR and main run on GitHub Actions** — real CI, which caught three bugs local
  development had masked, including a container that could not have started on SageMaker
- Five regression tests **proved** to fail on the regressions they target
- Real drift math against the real baseline, producing the three-verdict evidence
- A real load measurement that corrected two of my own guessed thresholds

**Written and validated, but never executed**
- Every Terraform resource. `plan`, `apply` and `destroy` have never run.
- Both state machines. No execution history exists.
- The endpoint, the canary, the rollback alarms, the dashboard, the scheduled drift job.

**Absent**
- The M2 auto-rollback recording and the M5 full retrain cycle — two named deliverables
  that need a live account.
- The dashboard screenshot.

The single blocker is AWS credentials. Everything above the line was built to be
verifiable without them, deliberately, and everything below is named rather than
approximated.

## Known gaps

Honest list. These are things that are wrong or missing right now, not a roadmap.

**Blocking, and the reason everything else is unverified**

- **No AWS credentials were available during the build**, so `terraform plan`,
  `apply` and `destroy` have never run. Three of M0's five acceptance criteria are
  consequently unmet. `evidence/` holds what could be produced without an account —
  see [`evidence/README.md`](./evidence/README.md), which leads with what is absent.

**M6 specifically**

- **Every AWS job has never run.** The plan comment, the ECR push, `terraform
  apply`, the endpoint smoke test and the promote step are all gated on
  `AWS_DEPLOY_ROLE_ARN` and skipped. The green runs prove the no-AWS half only.
- **The OIDC trust relationship is unverified.** The role and its `sub` conditions
  are written in `infra/modules/stack/iam.tf`, but no workflow has ever assumed it,
  so the trust policy could be wrong in a way only a real `AssumeRoleWithWebIdentity`
  would reveal.
- **Actions are pinned to major version tags, not commit SHAs.** `@v4` still allows
  the tag to move. SHA pinning is the stricter posture and is the obvious hardening
  step.
- **`ruff format` is not enforced.** Lint is; formatting would have reformatted 31
  files in one commit, and the diff cost outweighed the benefit at this stage.

**M5 specifically**

- **No full retrain → gate → approve → canary cycle.** Half of M5's deliverable. The
  retrain state machine is written and its safety properties are tested — registration
  is always `PendingManualApproval`, and no endpoint API appears anywhere in the
  definition — but it has **never executed**.
- **No M5 Terraform.** The drift Lambda, its EventBridge schedule, the retrain state
  machine and the EventBridge rule on registry approval are **not written**. The drift
  code runs only via the CLI.
- **The drift reports are real, but the input is not production data.** The math runs
  against the actual M1 baseline and actual generated batches — no stubs — but the
  windows are locally-scored documents, not SageMaker data capture.
  `parse_capture_record` handles the capture envelope and is tested, but has never
  seen a real capture file.
- **Audit sampling is not implemented.** Routing a random slice of confidently
  auto-approved documents to review is the only mechanism that would put
  confidently-wrong documents into the feedback loop. Its absence is the largest
  substantive gap in the drift design, not just an unbuilt feature — see the
  sampling-bias section.
- **`ks_statistic` is tested but unused by the report.** It was removed from the input
  family after it produced false positives against a histogram-reconstructed baseline.
  It is kept for the case where both sides have real samples.

**M4 specifically**

- **No dashboard screenshot.** Half of M4's deliverable. The dashboard is defined in
  Terraform and validates, but a screenshot needs it deployed with real data behind
  it. The other half — the alarm inventory — is generated from the Terraform and is
  in `evidence/m4/`.
- **No metric has ever been emitted.** The nine custom metrics, their dimensions and
  the metric-math expressions that derive rates from them are all unverified against
  CloudWatch. A metric-math expression with a typo renders as a blank panel, and
  nothing local catches that.
- **No X-Ray trace has been captured.** Tracing is enabled on the state machine and
  the Lambdas (`enable_xray`), and the runbook documents how to query a trace by
  `correlation_id`, but the annotation that makes that query work is not yet set —
  X-Ray filters on annotations, and nothing currently calls `put_annotation`. **The
  runbook's trace query would return nothing as written.**
- **Per-stage latency is not a custom metric.** It comes from X-Ray segments and
  `AWS/States` rather than from `Intake/Platform`, so the dashboard shows end-to-end
  latency and per-stage timing lives in the trace view. Defensible, but it means the
  per-stage panel the assignment asks for is not on the dashboard.
- **Alarm thresholds are reasoned, not calibrated.** Every one has a documented
  rationale, but none has been checked against real traffic. The auto-approval floor
  of 70% comes from an ~88% golden-set rate; production could sit anywhere.

**M3 specifically**

- **Nothing is deployed.** The Terraform now exists and validates, but no state
  machine, table, Lambda or API has been created. Every claim below about runtime
  behaviour is a design claim, not an observation.
- **The reviewer API has no real authorisation model.** The route is `AWS_IAM`
  authorised so it is not open to the internet, but `reviewer_id` comes from the
  request body and is trusted. "Which humans may review which documents" is a real
  access-control question this does not answer. A shared API key would have looked
  like an answer and been worse.
- **The traces in `evidence/m3/` are from a local simulation, not a Step Functions
  execution.** The routing logic, validator, correction flow and idempotency
  semantics are genuinely exercised; Textract, Bedrock, DynamoDB and Step Functions
  itself are stubbed. `TestSimulatorMatchesAsl` asserts the simulator has not
  diverged from the deployed definition, which is what makes the traces worth
  anything — but they are not the deliverable.
- **The ASL has never been validated by the service.**
  `aws stepfunctions validate-state-machine-definition` needs credentials.
  `tests/test_asl.py` checks 37 structural invariants, which is a stronger check than
  syntax in most respects, but it cannot catch an invalid intrinsic-function
  signature or a mistyped SDK integration ARN.
- **The `Extract` state's Bedrock request body is written for the Anthropic Messages
  API shape and is unverified against the service.** `parse_model_json` tolerates
  three response shapes for this reason, but the request side has one chance to be
  right.
- **No load or concurrency testing of the idempotency claim.** Two simultaneous
  deliveries of the same object should have exactly one win the conditional write.
  That is how DynamoDB conditional writes behave, but it is asserted, not observed.

**M2 specifically**

- **The headline deliverable does not exist.** M2 is graded on "a recorded bad
  deploy that rolled back on its own". No endpoint has been deployed, so there is
  no rollback recording. The canary policy and both rollback alarms are written and
  validated in Terraform; whether they actually fire is **unproven**.
- **The container image has never been built.** `docker build` was attempted and
  hung on this machine — the daemon became unresponsive and the build was
  abandoned rather than retried indefinitely. The `/ping` and `/invocations`
  contract *is* verified, by 41 tests against the Flask app directly, including
  readiness returning 503 when the model loads but cannot predict. What is
  unverified is the image: base image resolution, the dependency install inside it,
  and whether gunicorn starts under `serve`. `scripts/container_smoke.sh` exists to
  check exactly that and has never run.
- **The autoscaling target is derived from a sequential, in-process measurement on
  faster hardware than the target instance.** It is an upper bound on capacity with
  a guessed 0.35 derating factor. A real concurrent load test may disagree.
- **The latency alarm threshold (1500 ms) is 7× an in-process p99** that excludes
  HTTP framing and SageMaker overhead. The real p99 will be higher, making the
  effective multiple smaller than intended.
- **`termination_wait_in_seconds = 600`** keeps the old fleet alive after a shift,
  which is what makes rollback instant — but it also means a deploy holds double
  capacity for 10 minutes. That is a cost/safety trade I have not measured.

**M1 specifically**

- **Nothing has run on SageMaker.** `train.py` and `evaluate.py` implement the
  script-mode and Processing-job contracts (SM_CHANNEL_TRAIN, SM_MODEL_DIR,
  SM_OUTPUT_DATA_DIR) and run correctly locally, but no training job has been
  submitted, so no `model.tar.gz` exists in S3 and **no version has actually been
  written to a Model Package Group.** `register.py` has only been exercised
  `--dry-run`. The two-versions deliverable is proven as far as metrics and the
  registration payload; the registry API call itself is unverified.
- **The retrain gate does not read ECE.** It gates on macro-F1 margin plus a
  per-class floor. In the v1/v2 pair above it blocked the badly-calibrated
  candidate — but on the *margin*, incidentally, not because it noticed the
  calibration collapse. A candidate that improved macro-F1 by 0.03 while wrecking
  ECE would pass. Adding a calibration ceiling to the gate is the obvious fix and
  is not done.
- **`GATE_MIN_MACRO_F1_IMPROVEMENT = 0.02` is asserted, not derived.** It should
  come from the measured run-to-run variance on a 240-document golden set. I have
  not measured that variance, so the number is a plausible guess.
- **The golden set is synthetic and drawn from the same generator as training.**
  It is genuinely held out and non-overlap is asserted, but it is not an
  independent sample of the real world, so the absolute scores mean little. The
  *relative* comparison between versions is what the gate uses and that remains
  valid.

**Known-incomplete by design**

- **The `ci-deploy` role's apply permissions are scoped by resource TAG, not by
  ARN.** Terraform names resources at apply time, so an ARN allowlist would be
  either incomplete or a wildcard; every resource carries `project=intake` via
  `default_tags` and the policy conditions on that. `iam:CreateRole` is
  deliberately still absent — a CI role that can mint IAM roles can escalate to
  anything, and no tag condition prevents it. Bootstrapping IAM stays human.
  **Never exercised**: no workflow has assumed this role.
- **No Lambda IAM roles.** A role scoped to a function that doesn't exist can
  only be empty or wrong. Each is created with its function.
- **The state-machine role is trust-only** — no permission policy at all until
  M3 gives it something to run.

**Unvalidated risks I'd check first on the initial real apply**

- The `ci-deploy` `s3:ListBucket` condition allows both this environment's key
  prefix and `env:/*`, the latter because Terraform's S3 backend enumerates
  workspaces during `init`. That reasoning is untested against a live backend.
- The `aws:SourceArn` conditions on the SageMaker and Step Functions trust
  policies are scoped to account + region with a wildcard suffix, because the
  resources they name don't exist yet. They should be narrowed to concrete ARNs
  as M1–M3 create them.
- `kms:RetireGrant` was added to the key-admin actions specifically because ECR
  creates a grant on the key for the encrypted repository and deleting the repo
  needs to retire it. Untested — and it is the most likely cause if `make
  destroy` ever fails.

**Found by an audit after M7, and fixed — worth naming because they were invisible
to every test that existed**

- **Nothing triggered the pipeline.** No `aws_s3_bucket_notification { eventbridge =
  true }` existed, so S3 would never have published `Object Created`. Apply succeeded,
  the rule matched an event that was never delivered, and no document would ever have
  been processed.
- **Six of eleven alarms could never have fired.** Metrics were emitted with
  `[Environment, DocumentClass]` while every alarm queried `[Environment]`. CloudWatch
  treats the dimension set as part of a metric's identity and does not roll up, so
  those alarms would have sat in `INSUFFICIENT_DATA` permanently — and two tests
  passed throughout because they compared metric *names* only.
- **Human review was capped at 24 hours, not 7 days.** The execution timeout was
  shorter than the review state's, and a `States.Timeout` raised by the execution
  limit is not catchable — so the dead-letter path was unreachable and a document
  parked over a weekend was lost silently.
- **The canary could never have run.** Static resource names plus
  `create_before_destroy` meant every endpoint *update* — the only path a canary takes
  — failed with "cannot create already existing".
- **The dead-letter alarm notified nobody**, `make destroy` failed on a clean clone,
  and the Model Package Group was created outside Terraform so it survived teardown.

Each now has a test. The dimension mismatch is in the regression-proof harness, so it
is verified to fail on the real defect rather than merely asserted.

**Still genuinely absent**

- The M5 Terraform: the drift Lambda, its schedule, the retrain state machine and
  the EventBridge rule on registry approval. The retrain ASL exists and its
  placeholders are substituted by nothing.
- Audit sampling of confidently auto-approved documents — the single highest-value
  addition, and the fix for both the Q2 blind spot and the retraining bias.
- An X-Ray annotation on `correlation_id`, without which the runbook's
  trace-by-document query returns nothing.
- **Review-queue ageing is unmonitored.** Tasks expire at 7 days and then
  dead-letter, so an undrained queue is data loss on a deadline — but the right
  metric (age of the oldest pending review) needs a scheduled scan that does not
  exist. Two wrong alarms were written before this was admitted: one measuring
  arithmetic that evaluated to the auto-approved count, one reading a metric with no
  publisher. A documented gap beats an alarm that cannot fire.

---

## Guardrails this repo holds itself to

- No secrets, account IDs, or `AKIA...` keys committed. Account id comes from
  `data.aws_caller_identity`.
- No `iam:*` and no wildcard *grants*; least privilege, one role per component.
- Every AWS resource created by Terraform. No console clicks.
- No retraining that auto-deploys without a human-approved registry gate.
- Monitoring must measure model and business outcomes, not just CPU.
- `make destroy` leaves the account clean, and anything that survives it is
  named explicitly above.
