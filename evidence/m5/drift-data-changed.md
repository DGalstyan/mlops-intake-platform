# Drift report

**Verdict: DATA_CHANGED**

- Window: `deliberately shifted batch (longer documents, new vocabulary)` — 400 documents, 30 reviewed, 3 overridden
- Baseline snapshot: `sha256:0aba339dd3d872afd4295cb32d150f5e37d3d173259530c28f57f0cc93a5500d`
- Generated: 2026-07-30T15:16:38+00:00
- Triggers retrain: **no**

## Recommended action

DO NOT retrain on this signal alone. The input distribution moved but the model is still handling it — override rate and confidence are steady. Retraining here fits the new distribution's noise using review-sourced labels that are biased toward hard cases, which can make the model worse. Investigate what changed upstream (a new sender, a new scanner, a layout change) and consider whether the extraction schema or prompt needs updating. Raise the watch frequency; re-evaluate if the concept proxies start moving.

## Why this distinction matters

Input drift and model decay require different responses, and one of them is actively harmful when applied to the other. Retraining in response to input drift alone spends money fitting the new distribution's noise, using labels sourced from human review — which only sees low-confidence documents and is therefore biased toward hard cases. A single combined 'drift score' would map these opposite situations onto the same number and prescribe the same action for both.

## Input drift — BREACHED

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `psi_document_char_length` | 19.7589 | 0.25 | **yes** | significant shift |
| `median_shift_document_char_length` | 2.6204 | 0.25 | **yes** | document length (characters): median moved +262.0% (162 -> 586); documents got longer |
| `psi_document_token_count` | 19.1318 | 0.25 | **yes** | significant shift |
| `median_shift_document_token_count` | 2.6000 | 0.25 | **yes** | document length (tokens): median moved +260.0% (20 -> 72); documents got longer |

## Prediction drift — within thresholds

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `psi_predicted_class_mix` | 0.0001 | 0.25 | no | class mix matches the baseline |

## Concept drift — within thresholds

| Signal | Statistic | Threshold | Breached | Interpretation |
|---|---|---|---|---|
| `confidence_p10_decay` | 0.3539 | -0.1 | no | p10 confidence moved +35.4% vs baseline (0.731 -> 0.990) |
| `share_below_auto_approve_threshold` | 0.0150 | 0.3 | no | 1.5% of documents fell below the 0.8 auto-approve threshold |
| `override_rate_trend` | 0.1000 | 0.5 | no | override rate is 10.0% of 30 reviewed documents, but the baseline records no override-rate reference (it was built from training data, where nothing was reviewed). This becomes a usable signal once a production window has been captured as the new reference. |
