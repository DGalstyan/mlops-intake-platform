# M2 — Deployment

**Owner:** `deployment-release`  ·  **Skills:** `sagemaker-model-registry`,
`terraform-aws-conventions`  ·  **Grade tie-in:** Deployment & release safety (20%)

## Goal
A **recorded bad deploy that rolled back on its own.**

## Tasks
- [ ] Inference container (or documented managed image + `inference.py`) pushed to
      ECR; implement model_fn/input_fn/predict_fn/output_fn; immutable image tag.
- [ ] Real-time endpoint (or SageMaker Serverless — if serverless, document
      cold-start handling and what monitoring you lose).
- [ ] **Data capture** enabled (input + output, 100% in dev) → data-capture bucket.
- [ ] **Autoscaling** on a justified metric with a documented target-value reason.
- [ ] **Health/readiness contract** (`/ping`, `/invocations`) + a post-deploy
      **smoke test** that fails the release if the contract breaks.
- [ ] Endpoint updates are **canary or linear** with **auto-rollback** wired to
      CloudWatch alarms (latency / 5xx / model metric).
- [ ] Only deploy a registry version whose status is Approved.

## Acceptance criteria (Deliverable)
- [ ] Deliberately deploy a broken model version; the alarm trips and the deploy
      **auto-rolls-back**; capture CloudWatch + deployment history to `evidence/`.
- [ ] Smoke test runs post-deploy and gates the release.
- [ ] Autoscaling metric + target justified in README; cost note included.

## Definition of done
`evidence/` contains the recorded bad-deploy → auto-rollback; `mlops-reviewer`
confirms rollback is automatic (alarm-driven), not a manual command, and artifacts
are immutable.
