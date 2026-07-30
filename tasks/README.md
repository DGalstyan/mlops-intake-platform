# Task Breakdown — Document-Intake MLOps Platform

Each milestone is independently gradeable. **Ship them in order** — a complete
M0–M4 beats a half-finished M0–M6. Each task file lists the work, the owning
subagent, the skills to load, and the acceptance criteria tied to the
assignment's named deliverable.

## How to drive the build
1. Read `.claude/skills/mlops-project-conventions` (guardrails + repo layout).
2. Work one milestone file at a time. Delegate its tasks to the owning subagent.
3. Before marking a milestone done, run the **`mlops-reviewer`** agent against it.
   A milestone is done only when its deliverable exists in `evidence/` and the
   reviewer finds no instant point-losers.

## Milestone index & grade weight
| Milestone | Owner agent | Deliverable | Grade tie-in |
|---|---|---|---|
| [M0 Foundations](./M0-foundations.md) | iac-terraform | `terraform plan` builds the whole stack on a clean account | IaC & reproducibility 15% |
| [M1 Training + Registry](./M1-training-registry.md) | model-training, synthetic-data | two distinguishable registry versions | Code quality 15% |
| [M2 Deployment](./M2-deployment.md) | deployment-release | recorded bad deploy that auto-rolled back | Release safety 20% |
| [M3 Orchestration + HITL](./M3-orchestration-hitl.md) | orchestration | trace of one auto-approved + one corrected doc | (feeds all) |
| [M4 Observability](./M4-observability.md) | observability | dashboard shot + alarm inventory | Observability 20% |
| [M5 Drift + Retraining](./M5-drift-retraining.md) | drift-retraining | drift report + full retrain→gate→approve→canary | Drift & retraining 20% |
| [M6 CI/CD](./M6-cicd.md) | cicd | green PR run + green main run | (cross-cutting) |
| [Submission](./M7-submission.md) | mlops-reviewer | README, runbook, evidence, clean destroy | Docs & judgement 10% |

## Global guardrails (apply to every task)
- No secrets / account IDs / `AKIA...` keys in the repo.
- No `iam:*` or `Resource: "*"`.
- No console-created resources — everything in Terraform.
- `make destroy` leaves the account clean.
- README documents what was built, not intentions.
- Keep total AWS spend under ~$15 for a full run; tear down when done.
