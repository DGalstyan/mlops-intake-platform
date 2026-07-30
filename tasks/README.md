# Task breakdown — document-intake MLOps platform

Each milestone is independently gradeable. **Ship them in order** — a complete
M0–M4 beats a half-finished M0–M6. Each file lists the work and the acceptance
criteria tied to the assignment's named deliverable.

## How to drive the build

1. Read `docs/conventions.md` for the guardrails and repo layout.
2. Work one milestone file at a time.
3. Before marking a milestone done, audit it against the rubric in
   `docs/ASSIGNMENT.md` §4. A milestone is done only when its named deliverable
   exists in `evidence/` and no instant point-losers remain.

## Milestone index & grade weight

| Milestone | Deliverable | Grade tie-in |
|---|---|---|
| [M0 Foundations](./M0-foundations.md) | `terraform plan` builds the whole stack on a clean account | IaC & reproducibility 15% |
| [M1 Training + Registry](./M1-training-registry.md) | two distinguishable registry versions | Code quality 15% |
| [M2 Deployment](./M2-deployment.md) | recorded bad deploy that auto-rolled back | Release safety 20% |
| [M3 Orchestration + HITL](./M3-orchestration-hitl.md) | trace of one auto-approved + one corrected doc | (feeds all) |
| [M4 Observability](./M4-observability.md) | dashboard shot + alarm inventory | Observability 20% |
| [M5 Drift + Retraining](./M5-drift-retraining.md) | drift report + full retrain→gate→approve→canary | Drift & retraining 20% |
| [M6 CI/CD](./M6-cicd.md) | green PR run + green main run | (cross-cutting) |
| [Submission](./M7-submission.md) | README, runbook, evidence, clean destroy | Docs & judgement 10% |

## Global guardrails (apply to every task)

- No secrets / account IDs / `AKIA...` keys in the repo.
- No `iam:*` or `Resource: "*"` grants.
- No console-created resources — everything in Terraform.
- `make destroy` leaves the account clean.
- The README documents what was built, not intentions.
- Keep total AWS spend under ~$15 for a full run; tear down when done.
