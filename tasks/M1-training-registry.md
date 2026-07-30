# M1 — Reproducible Training & the Model Registry

**Owner:** `model-training` (+ `synthetic-data`)  ·  **Skills:**
`sagemaker-model-registry`, `mlops-project-conventions`  ·  **Grade tie-in:**
Code quality (15%), reproducibility

> Model accuracy is an explicit **non-goal**. Optimize reproducibility, honest
> metrics, and lineage.

## Tasks
- [ ] (synthetic-data) Generator for the 4 classes + JSON schemas in `schemas/`;
      emit a data snapshot id; produce a frozen golden set held out of training.
- [ ] SageMaker Training Job (script mode) → `model.tar.gz` + `metrics.json`;
      deterministic (seeds, pinned deps); log git SHA + snapshot id + image digest.
- [ ] Keep the classifier behind a swappable model interface (fit/predict/
      predict_proba/save/load).
- [ ] SageMaker Processing job evaluates on the golden set (NOT in training data —
      assert non-overlap); emit per-class F1, macro-F1, and a calibration metric
      (ECE or reliability bins).
- [ ] **Baseline statistics artifact**: prediction priors, input length/token
      distributions, feature/embedding summaries, confidence histogram — versioned;
      justify contents in README (contract with drift-retraining).
- [ ] Register into a Model Package Group with `PendingManualApproval`, metrics
      attached, lineage to snapshot + git SHA.
- [ ] Unit-test metric + calibration math against known inputs.

## Acceptance criteria (Deliverable)
- [ ] Two training runs produce **two distinguishable versions** in the registry
      (different metrics), both PendingManualApproval, both with lineage.
- [ ] Golden-set non-overlap asserted in code.
- [ ] Baseline artifact exists with documented, versioned schema.

## Definition of done
`mlops-reviewer` confirms two distinguishable versions, lineage present, and the
baseline artifact stores *distributions* (not just accuracy).
