# M5 — Drift Detection & the Retraining Loop

**Owner:** `drift-retraining`  ·  **Skills:** `drift-detection-methods`,
`sagemaker-model-registry`, `stepfunctions-intake-asl`  ·  **Grade tie-in:**
Drift & retraining loop (20%)

## Goal
A drift report from a deliberately shifted batch, plus one full
retrain → gate → approve → canary cycle.

## Tasks
- [ ] Scheduled job (Processing job or Lambda) reads data-capture and computes
      drift vs the M1 baseline:
      - input drift (PSI / KS / embedding-based),
      - prediction drift (class distribution shift),
      - concept-drift proxy (override-rate trend, confidence decay).
- [ ] Report **distinguishes "data changed" from "model got worse"** and explains
      why treating them the same is a bug; labels each signal + recommended action.
- [ ] Breach → SNS + written report to S3; optionally triggers the retrain state
      machine.
- [ ] Retrain state machine: train → evaluate → **gate** (beat champion by a
      defined margin on the golden set AND no single class below a floor) →
      register (PendingManualApproval) → notify.
- [ ] Human registry approval fires an EventBridge rule → M2 canary deploy. **No
      auto-deploy without the gate.**
- [ ] README: the **sampling-bias** analysis (review only sees low-confidence docs)
      and your mitigation; what it does to the model after three retrain cycles.

## Acceptance criteria (Deliverable)
- [ ] Drift report from a deliberately shifted batch in `evidence/`, separating
      data drift from decay.
- [ ] One full retrain → gate → approve → canary cycle demonstrated.
- [ ] Gate margin + per-class floor are explicit, in config, justified in README.

## Definition of done
`mlops-reviewer` confirms the data-vs-decay distinction, a defensible gate, no
auto-deploy without a human, and an honest sampling-bias section.
