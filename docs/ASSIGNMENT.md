# Take-Home Assignment — AI Platform / MLOps Engineer (Python + AWS)

**Role:** Python AI Engineer with MLOps experience
**Focus:** Model deployment, orchestration, observability, drift detection, retraining, IaC, CI/CD
**Expected effort:** 10–14 hours for the core path; the stretch goals are deliberately more than anyone can finish
**Budget guardrail:** the whole thing must run for **under ~$15 of AWS spend** if you tear down when done

---

## 1. Context

You've joined a team that ships a **document intake platform**. Research engineers have handed you a notebook that classifies incoming documents and a prompt that extracts structured fields from them. It works on their laptop. Nothing else exists.

Your job is the part they didn't do: turn it into a system that a customer can send 50,000 documents through, that a human can correct when it's wrong, that tells you when it starts degrading, and that can be retrained and redeployed without a human SSHing into anything.

**Explicit non-goal: model accuracy.** The classifier can be a TF-IDF + linear model and that's fine. We are not evaluating your ML. We are evaluating whether you can build the platform around a model. A submission with a 0.72 macro-F1 model and a flawless deployment/monitoring/rollback story scores far higher than a 0.95 model behind a hand-deployed endpoint.

---

## 2. The system to build

```
S3 upload ──► EventBridge ──► Step Functions "intake" state machine
                                   │
                                   ├─ 1. OCR            (Textract)
                                   ├─ 2. Classify       (SageMaker endpoint — your model)
                                   ├─ 3. Route          (confidence + business-rule gate)
                                   ├─ 4. Extract        (Bedrock, class-specific prompt + JSON schema)
                                   ├─ 5. Validate       (schema + field-level rules)
                                   └─ 6a. Auto-approve  ──► results store
                                       6b. Human review ──► review queue ──► corrections ──► labelled data
                                                                                    │
                     ┌──────────────────────────────────────────────────────────────┘
                     ▼
        Step Functions "retrain" state machine
          train ──► evaluate vs champion ──► gate ──► Model Registry (PendingManualApproval)
                                                              │ approval event
                                                              ▼
                                              canary deploy ──► auto-rollback on alarm
```

### Domain

Four document classes: `invoice`, `medical_report`, `id_document`, `correspondence`.
Each class has its own extraction schema (e.g. invoice → `invoice_number`, `total_amount`, `currency`, `due_date`, `vendor_name`).

You may generate synthetic documents — a generator is expected as part of the deliverable so a reviewer can reproduce your run from zero. Do not spend more than an hour on data.

---

## 3. Milestones

Each milestone is independently gradeable. Ship them in order; a complete M0–M4 beats a half-finished M0–M6.

### M0 — Foundations (IaC bootstrap)
- Terraform with **remote state** (S3 + DynamoDB lock or S3 native locking), workspaces or `-var-file` per environment (`dev`, `staging`).
- All AWS resources in this assignment are created by Terraform. **Console clicks are a scored failure**; if you click something to explore, import it or delete and codify it.
- ECR repository, S3 buckets (raw / processed / artifacts / data-capture, with lifecycle rules), KMS key, IAM roles scoped per-component.
- `make bootstrap`, `make plan`, `make apply`, `make destroy`.

**Deliverable:** `terraform plan` on a clean account produces the whole stack. No manual steps beyond credentials.

### M1 — Reproducible training and the Model Registry
- SageMaker Training Job (script mode is fine) producing `model.tar.gz` + `metrics.json`.
- A SageMaker Processing job that evaluates the candidate on a **frozen golden set** that is *not* part of training data, and emits per-class F1, macro-F1, and a calibration metric (ECE or reliability bins).
- Training must also emit a **baseline statistics artifact** — the distributions you'll later compare production traffic against. Decide what belongs in it and justify it in your README.
- Register the model in a **Model Package Group** with `ModelApprovalStatus = PendingManualApproval`, metrics attached, and lineage back to the data snapshot + git SHA.

**Deliverable:** two training runs produce two versions in the registry with distinguishable metrics.

### M2 — Deployment
- Custom inference container (or documented use of a managed image + `inference.py`) pushed to ECR.
- Real-time endpoint with:
  - **data capture** enabled (input + output, 100% in dev), landing in S3
  - **autoscaling** on a justified metric, with a documented reason for the target value
  - a **health/readiness contract** and a smoke test that runs post-deploy
- Endpoint updates must be **canary or linear** with **automatic rollback** wired to CloudWatch alarms. Prove the rollback works: deliberately deploy a broken model version and include the evidence.
- Cost note: SageMaker Serverless Inference is an acceptable and encouraged choice — but then explain how you handle cold starts and what you lose (hint: some monitoring features).

**Deliverable:** a recorded bad deploy that rolled back on its own.

### M3 — Orchestration and human-in-the-loop
- Step Functions state machine implementing the intake flow. Prefer **direct SDK integrations** (Textract, SageMaker Runtime, Bedrock, DynamoDB) over Lambda glue; justify every Lambda you do keep.
- Explicit `Retry` (with jitter) and `Catch` on every fallible state. Throttling from Bedrock/Textract must not lose a document.
- **Idempotency**: the same S3 object delivered twice must not produce two results or two review tasks.
- Human review implemented with `.waitForTaskToken`: low-confidence or schema-failing documents park in a review queue (DynamoDB), a reviewer submits corrections through a small API, the task token resumes the workflow.
- Corrections are written back as **labelled training data** with reviewer id, timestamp, original prediction, and corrected label.
- A dead-letter path for documents that fail everything, with enough context to debug one.

**Deliverable:** end-to-end trace of one auto-approved document and one human-corrected document.

### M4 — Observability
- Structured JSON logging with a `correlation_id` propagated from S3 event through every step, including into Bedrock request metadata.
- **Custom CloudWatch metrics** in your own namespace. At minimum:
  - `AutoApprovalRate`, `HumanOverrideRate`, `ConfidenceP50` / `ConfidenceP10`
  - `SchemaValidationFailureRate`
  - `EndToEndLatencyP95`, per-stage latency
  - `LLMInputTokens` / `LLMOutputTokens` / `EstimatedCostPerDocument`
- A **CloudWatch dashboard defined in Terraform** that a non-engineer could read: model health, pipeline health, business outcome, cost.
- Alarms with meaningful thresholds → SNS. For each alarm, one sentence in the README: what breaks, who's paged, what the first response is.
- X-Ray or OTel tracing across the state machine.
- **Discuss in your README:** which of these metrics actually measures *model quality*, and which only measure *system health*? What's your proxy for accuracy in production, given you have no ground truth for most documents?

**Deliverable:** dashboard screenshot + alarm inventory.

### M5 — Drift detection and the retraining loop
- A scheduled job (Processing job or Lambda) that reads the data-capture files and computes drift against the M1 baseline. Cover at least:
  - **input drift** (feature/text distribution — PSI, KS, or embedding-based, your call)
  - **prediction drift** (class distribution shift)
  - **concept drift proxy** (human override rate trend, confidence decay)
- Distinguish **"data changed"** from **"model got worse"** in your report and explain why treating them the same is a bug.
- Drift breach → SNS + a written report to S3, and optionally triggers the retrain state machine.
- Retrain state machine: train → evaluate → **gate** (candidate must beat champion by a margin you define on the golden set, and not regress any single class below a floor) → register → notify. Registry approval is a **human** action; approval fires an EventBridge rule that triggers the canary deploy from M2.
- Handle the trap: your retraining data comes from human review, which only sees **low-confidence** documents. Explain the sampling bias and what you'd do about it.

**Deliverable:** drift report from a deliberately shifted batch, plus one full retrain → gate → approve → canary cycle.

### M6 — CI/CD
- GitHub Actions (or equivalent) using **OIDC, no long-lived AWS keys**.
- PR: lint, type-check, unit tests, `terraform validate` + `plan` posted as a comment, container build.
- Main: build/push image, `terraform apply` to dev, integration test against the live pipeline, promote.
- A separate manually-triggered retrain workflow.
- At least one test that would have caught a real regression (e.g. inference contract test, ASL definition validation, schema-compatibility test between model output and downstream consumer).

**Deliverable:** a green PR run and a green main run.

### Stretch (pick at most one, only if the rest is solid)
- Shadow/A-B testing with SageMaker production variants and a real traffic split analysis.
- Multi-model or multi-tenant endpoint with per-tenant metrics.
- Batch/backfill path (Batch Transform or Step Functions distributed map) reusing the same container.
- Prompt/model registry for the Bedrock side, with versioned prompts and offline LLM eval.
- Feature/embedding store with point-in-time correctness.

---

## 4. What we're actually grading

| Area | Weight | What "excellent" looks like |
|---|---|---|
| Deployment & release safety | 20% | Canary + proven auto-rollback, immutable artifacts, no manual step anywhere |
| Observability | 20% | Metrics that map to business outcomes, not just CPU; dashboard is legible; alarms are actionable |
| Drift & retraining loop | 20% | Correctly separates data drift from performance decay; honest about sampling bias; gate is defensible |
| IaC & reproducibility | 15% | Clean Terraform, least-privilege IAM, `destroy` actually leaves nothing behind |
| Code quality | 15% | Typed, tested, modular Python; the AI-specific parts are swappable without touching the plumbing |
| Docs, cost & judgement | 10% | Decision log with rejected alternatives; real cost breakdown; clear about what you skipped and why |

### Things that lose points fast
- Secrets, account IDs, or `AKIA...` keys in the repo.
- `iam:*` / `Resource: "*"` policies "for now".
- Resources created in the console.
- A monitoring section that only shows CPU/invocations and calls it model monitoring.
- Retraining that automatically deploys to production with no gate.
- A README that describes intentions rather than what you built.

### Things that gain points
- Saying "I chose X over Y because Z, and here's when I'd flip that decision."
- Deleting your own over-engineering and saying why.
- Load-test numbers, even bad ones.
- An honest "here's what's broken and what I'd fix next" section.

---

## 5. Submission

1. Git repo, meaningful commit history (not one `initial commit`).
2. `README.md`: architecture diagram, `make`-based quickstart from empty AWS account, decision log, cost table, known gaps.
3. `docs/runbook.md`: one page — endpoint is 5xx-ing at 3am, what do I do?
4. Evidence folder: dashboard screenshot, rollback proof, drift report, one full trace, CI runs.
5. A 5–10 minute Loom-style walkthrough (optional but strongly weighted — talk over the dashboard, not the code).
6. `make destroy` must work. Confirm your account is clean.

---

## 6. Live discussion (we'll ask these)

Prepare to defend, on a whiteboard, without your repo:

1. Your endpoint p99 triples with no code change and no traffic change. Walk me through the first 15 minutes.
2. A customer says extraction quality dropped last week. Your drift metrics are all green. What now? Which of your metrics *should* have caught it, and why didn't it?
3. Bedrock deprecates the model version you pinned, with 30 days' notice. What has to change, and how much of it is automated in your design?
4. Why did you gate registry approval on a human? When would you remove the human, and what evidence would you need?
5. You need to support 40 document types instead of 4, added by a customer success team, not engineers. What survives your current design and what gets rewritten?
6. Cost has doubled. Show me where you'd look, in order.
7. Where's the sampling bias in your retraining data, and what does it do to your model after three retrain cycles?
