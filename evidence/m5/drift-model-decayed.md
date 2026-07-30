# Drift report

**Verdict: MODEL_DECAYED**

- Window: `unshifted inputs, override rate 10% -> 45%` — 240 documents, 40 reviewed, 18 overridden
- Baseline snapshot: `sha256:0aba339dd3d872afd4295cb32d150f5e37d3d173259530c28f57f0cc93a5500d`
- Generated: 2026-07-30T15:16:38+00:00
- Triggers retrain: **yes**

## Recommended action

RETRAIN CANDIDATE. Inputs look unchanged but the model is getting the reviewed slice wrong more often and/or is less confident. The relationship moved underneath a stable distribution, which is precisely what input-drift tests cannot see. Trigger the retrain state machine; the gate decides whether the candidate is actually better.

## Why this distinction matters

Input drift and model decay require different responses, and one of them is actively harmful when applied to the other. Retraining in response to input drift alone spends money fitting the new distribution's noise, using labels sourced from human review — which only sees low-confidence documents and is therefore biased toward hard cases. A single combined 'drift score' would map these opposite situations onto the same number and prescribe the same action for both.

## Input drift — within thresholds

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `psi_document_char_length` | 0.0092 | 0.25 | no | stable |
| `median_shift_document_char_length` | 0.0370 | 0.25 | no | document length (characters): median moved +3.7% (162 -> 168); documents got longer |
| `psi_document_token_count` | 0.0019 | 0.25 | no | stable |
| `median_shift_document_token_count` | 0.0000 | 0.25 | no | document length (tokens): median moved +0.0% (20 -> 20); documents got shorter |

## Prediction drift — within thresholds

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `psi_predicted_class_mix` | 0.0008 | 0.25 | no | class mix matches the baseline |

## Concept drift — BREACHED

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `confidence_p10_decay` | 0.0000 | -0.1 | no | p10 confidence moved +0.0% vs baseline (0.731 -> 0.731) |
| `share_below_auto_approve_threshold` | 0.1250 | 0.3 | no | 12.5% of documents fell below the 0.8 auto-approve threshold |
| `override_rate_trend` | 3.5000 | 0.5 | **yes** | override rate moved +350.0% (10.0% -> 45.0%) |
