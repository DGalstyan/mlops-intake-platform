# Drift report

**Verdict: NO_DRIFT**

- Window: `unshifted golden set (control)` — 240 documents, 30 reviewed, 3 overridden
- Baseline snapshot: `sha256:0aba339dd3d872afd4295cb32d150f5e37d3d173259530c28f57f0cc93a5500d`
- Generated: 2026-07-30T15:16:38+00:00
- Triggers retrain: **no**

## Recommended action

No action. Inputs and model behaviour both look like the baseline.

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

## Concept drift — within thresholds

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `confidence_p10_decay` | 0.0000 | -0.1 | no | p10 confidence moved +0.0% vs baseline (0.731 -> 0.731) |
| `share_below_auto_approve_threshold` | 0.1250 | 0.3 | no | 12.5% of documents fell below the 0.8 auto-approve threshold |
| `override_rate_trend` | 0.1000 | 0.5 | no | override rate is 10.0% of 30 reviewed documents, but the baseline records no override-rate reference (it was built from training data, where nothing was reviewed). This becomes a usable signal once a production window has been captured as the new reference. |
