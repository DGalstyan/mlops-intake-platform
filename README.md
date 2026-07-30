# Document-Intake MLOps Platform — Claude Code Asset Pack

This repo is a **driver kit** for building the AI Platform / MLOps take-home
assignment (document intake: S3 → EventBridge → Step Functions → SageMaker →
Bedrock → human review → drift → retrain → canary deploy). It does **not**
contain the implementation — it contains the Claude Code **agents**, **skills**,
and **task breakdown** that let you (with Claude) build the implementation
milestone by milestone, while keeping the assignment's guardrails and grading
rubric loaded at all times.

Drop this at the root of your assignment repo. Claude Code auto-discovers
`.claude/agents/` and `.claude/skills/`; the `tasks/` folder is your plan of record.

## What's in here

### Subagents — `.claude/agents/` (delegate a milestone to a specialist)
| Agent | Owns | Milestone |
|---|---|---|
| `iac-terraform` | Terraform, remote state, IAM least-privilege, buckets, KMS, ECR | M0 (+ all infra) |
| `synthetic-data` | Document generator, class JSON schemas, golden set, shifted batch | supports M1/M5 |
| `model-training` | Training + eval, calibration, baseline artifact, Model Registry | M1 |
| `deployment-release` | Inference container, endpoint, data capture, canary + auto-rollback | M2 |
| `orchestration` | Step Functions intake, idempotency, `.waitForTaskToken` review, DLQ | M3 |
| `observability` | Structured logs, custom metrics, TF dashboard, alarms, tracing | M4 |
| `drift-retraining` | Drift math, retrain state machine, gate, sampling-bias | M5 |
| `cicd` | GitHub Actions, OIDC, PR/main/retrain workflows, regression test | M6 |
| `mlops-reviewer` | Read-only auditor vs the rubric + "things that lose points" | every milestone |

### Skills — `.claude/skills/` (loaded on demand for the "how")
- `mlops-project-conventions` — guardrails, repo layout, coding standards, README format. **Read first, every task.**
- `terraform-aws-conventions` — state, modules, least-privilege IAM, buckets, KMS.
- `sagemaker-model-registry` — training, golden-set eval, calibration, baseline artifact, registry + lineage.
- `stepfunctions-intake-asl` — direct SDK integrations, retry+jitter, catch, idempotency, task-token review, DLQ.
- `cloudwatch-observability` — correlation_id logging, business/model/cost metrics, TF dashboard, alarms.
- `drift-detection-methods` — PSI/KS/embedding drift, data-vs-decay, retrain gate, sampling bias.

### Tasks — `tasks/` (the plan of record)
`tasks/README.md` indexes milestone files `M0`–`M7`, each with tasks, the owning
agent, the skills to load, and acceptance criteria tied to the assignment's named
deliverables and grade weights.

## How to use it

1. Open the assignment repo (this pack at its root) in Claude Code.
2. Read `tasks/README.md`, then work one milestone file top to bottom.
3. Delegate to the owning subagent, e.g.:
   > "Use the **iac-terraform** agent to implement M0 from `tasks/M0-foundations.md`."
4. When a milestone's tasks are checked, run the auditor:
   > "Use the **mlops-reviewer** agent to audit M0 against the rubric."
   Fix its findings before moving on.
5. Repeat M1 → M6, then `tasks/M7-submission.md` for README, runbook, evidence,
   and a clean `make destroy`.

Ship milestones **in order** — a complete M0–M4 beats a half-finished M0–M6.

## Guardrails baked into every agent and skill
- No secrets / account IDs / `AKIA...` keys in the repo.
- No `iam:*` or `Resource: "*"` — least-privilege, one role per component.
- No console-created resources — everything in Terraform.
- No retraining that auto-deploys without a human-approved gate.
- Monitoring must measure business/model outcomes, not just CPU.
- `make destroy` leaves the account clean; keep a full run under ~$15.

## Grade map these assets target
Deployment & release safety 20% · Observability 20% · Drift & retraining 20% ·
IaC & reproducibility 15% · Code quality 15% · Docs, cost & judgement 10%.

> These are scaffolding assets to steer the build — they intentionally contain no
> AWS resources or credentials, so nothing here spends money or needs teardown.
