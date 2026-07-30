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

This is the honest current state. Only M0 is implemented; nothing below it has
been written yet, and no AWS resource has ever been applied from this repo.

| Milestone | Status | What exists |
|---|---|---|
| **M0** Foundations (IaC) | **Code complete, never applied** | `infra/` — state backend, KMS, ECR, 4 buckets, per-component IAM. `terraform validate` + `fmt` clean on all three roots. |
| M1 Training + Registry | Not started | — |
| M2 Deployment | Not started | — |
| M3 Orchestration + HITL | Not started | — |
| M4 Observability | Not started | — |
| M5 Drift + Retraining | Not started | — |
| M6 CI/CD | Not started | `.github/workflows/` is empty |

**What "never applied" means:** `terraform plan` has not been run against a real
account, because no AWS credentials are configured on the machine this was built
on. So `fmt` and `validate` are verified; `plan`, `apply` and `destroy` are
**not**. `evidence/` is empty for the same reason. Treat the cost table below as
calculated-from-price-lists, not observed-on-a-bill.

The per-milestone plan of record is in [`tasks/`](./tasks/), with an audit of M0
against the grading rubric recorded in
[`tasks/M0-foundations.md`](./tasks/M0-foundations.md).

---

## Architecture

Status key: `[x]` built · `[ ]` not built yet.

```
[ ] S3 upload ──► EventBridge ──► Step Functions "intake" state machine
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

[x] Foundations that everything above sits on (infra/):
      KMS key (per env) · ECR repo (immutable tags) · 4 S3 buckets
      (raw / processed / artifacts / data-capture) · per-component IAM roles
      · S3 remote state with native locking · GitHub Actions OIDC provider
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
| M2 | SageMaker inference endpoint | **The dominant cost.** A real-time `ml.m5.large` is ~$0.115/hr, so ~$2.76/day left running. Serverless Inference is the cheaper choice for a graded run that idles most of the time — at the price of cold starts and losing some monitoring surface. This decision gets made and recorded at M2. |
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
