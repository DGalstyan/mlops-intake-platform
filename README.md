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
repo** — there are no credentials on the machine it was built on. M0–M3 are written
and tested locally; M4–M6 are not started.

| Milestone | Status | What exists |
|---|---|---|
| **M0** Foundations (IaC) | **Code complete, never applied** | `infra/` — state backend, KMS, ECR, 4 buckets, per-component IAM. `terraform validate` + `fmt` clean on all three roots. |
| **M1** Training + Registry | **Code complete, runs locally; never run on SageMaker** | Generator + 4 schemas, swappable model interface, train/evaluate entrypoints, calibration + ECE, baseline artifact, registry assembly. Tested and type-checked locally. |
| **M2** Deployment | **Code complete; endpoint never deployed, image never built** | Inference handlers + serving layer with verified `/ping`/`/invocations` contract, Dockerfile, endpoint Terraform with canary + alarm-driven auto-rollback, data capture, measured autoscaling target, approved-only resolver, post-deploy smoke test. |
| **M3** Orchestration + HITL | **ASL + handlers complete and tested; NO Terraform, never executed** | 25-state intake ASL with retry/jitter/catch throughout, idempotency ledger, `.waitForTaskToken` review, corrections as labelled data, dead-letter path. Prompts rendered from schemas. Local simulation produces the two required traces. |
| M4 Observability | Not started | — |
| M5 Drift + Retraining | Not started | — |
| M6 CI/CD | Not started | `.github/workflows/` is empty |

**What "never applied" means:** `terraform plan` has not been run against a real
account, because no AWS credentials are configured. So `fmt` and `validate` are
verified; `plan`, `apply` and `destroy` are **not**. Treat the cost table below as
calculated-from-price-lists, not observed-on-a-bill.

**What is in `evidence/` is therefore local, and labelled as such.** `m1/` holds real
training and evaluation output. `m2/` holds a real load measurement but **no rollback
recording** — M2's actual deliverable. `m3/` holds traces from a local *simulation*,
not a Step Functions execution. Each evidence folder states its own caveat; none of
them are a substitute for a run against AWS.

The per-milestone plan of record is in [`tasks/`](./tasks/), with an audit of M0
against the grading rubric recorded in
[`tasks/M0-foundations.md`](./tasks/M0-foundations.md).

---

## Architecture

Status key: `[x]` written and tested · `[~]` partially written · `[ ]` not started.
Nothing here has been deployed.

```
[~] S3 upload ──► EventBridge ──► Step Functions "intake" state machine
    (ASL + handlers written and tested; no Terraform, so nothing deploys)
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
[ ]     Step Functions "retrain" state machine
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
make test            # 261 tests, mypy-strict clean
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
system-health discussion belongs to M4 and is not written yet.

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
| M4 | CloudWatch custom metrics, dashboards | Per-metric monthly + dashboard fee. Small but not free at ~12 custom metrics. |
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

There are six, and they are inventoried with file:line and the AWS restriction
behind each in `docs/decisions.md`. Summary: three are **KMS key-policy**
statements, where `"*"` is AWS's documented spelling of "this key" and the key's
ARN cannot reference itself without a cycle; three are
`ecr:GetAuthorizationToken`, which has no resource type in the ECR IAM
reference. There is additionally one `Principal: "*"` / `Action: "s3:*"` in the
bootstrap root — that one is a **Deny** rejecting non-TLS requests to the state
bucket, which is the point.

There is no `iam:*`, no `Action: "*"` grant, and no service-level action
wildcard anywhere.

---

## Known gaps

Honest list. These are things that are wrong or missing right now, not a roadmap.

**Blocking, and the reason everything else is unverified**

- **No AWS credentials were available during the build**, so `terraform plan`,
  `apply` and `destroy` have never run. Three of M0's five acceptance criteria
  are consequently unmet, and `evidence/` is empty. This is the first thing to
  fix.

**M3 specifically**

- **The M3 infrastructure does not exist.** The ASL definition, the three Lambda
  handlers and the prompt renderer are written and tested, but the Terraform that
  would deploy them — the state machine, five DynamoDB tables, three Lambda
  functions, the EventBridge rule, the review API Gateway and the SQS dead-letter
  queue — is **not written**. This is the largest single gap in the repo.
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

- **The `ci-deploy` role cannot run `terraform apply`.** It can push images and
  read/write its own state key, and nothing more. Granting the create
  permissions for resources that don't exist yet would have meant wildcards, so
  the policy grows per-milestone. M6 closes it; until then the deploy path is a
  human running `make apply`.
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

**Not started**

- `docs/runbook.md` (the 3am 5xx page) does not exist.
- `src/`, `schemas/`, `statemachines/`, `tests/` are empty.
- No CI workflows.

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
