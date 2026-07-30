# M5 evidence — drift detection

**These reports are real.** Unlike the M2 and M3 evidence, nothing here is a
simulation or a stub: the drift math runs against the actual M1 baseline artifact and
the actual generated batches, using the same code the scheduled job would run. What
is missing is only the *scheduling* — no Lambda has been deployed and no report has
been written to S3.

## The three scenarios

| Scenario | Input | Prediction | Concept | Verdict | Retrain? |
|---|---|---|---|---|---|
| control (unshifted golden set) | ok | ok | ok | `NO_DRIFT` | **no** |
| deliberately shifted batch | BREACH | ok | ok | `DATA_CHANGED` | **no** |
| stable inputs, overrides 10% → 45% | ok | ok | BREACH | `MODEL_DECAYED` | **yes** |

## The middle row is the point

The shifted batch produces **enormous** input drift and the correct answer is still
**do not retrain**:

| Signal | Value | Reading |
|---|---|---|
| `psi_document_char_length` | **19.8** | ~79x the "significant shift" threshold of 0.25 |
| `median_shift_document_char_length` | **+262.0%** | documents got dramatically longer |
| `psi_predicted_class_mix` | 0.0001 | class mix **unchanged** |
| `confidence_p10_decay` | +35.4% | confidence went **up**, not down |
| `share_below_auto_approve_threshold` | 1.5% | almost nothing routed to humans |

The world changed and the model generalised. Retraining here would spend money to fit
the new distribution's noise, using labels sourced from human review — which only
covers the low-confidence slice. **You can make the model worse by responding to a
signal that did not require a response.** That is why the verdict is `DATA_CHANGED`
and `should_trigger_retrain` is `false`.

The third row is the mirror image: identical inputs, no prediction shift, but
reviewers overriding 45% instead of 10%. Nothing an input-drift test can see, and it
is the case that justifies a retrain.

## Two false positives found and fixed

Both were caught by running the **control** — an unshifted window that must report
`NO_DRIFT`. A detector that fires on unchanged data is worse than none, because it
gets muted and takes the real signal with it.

**1. The KS statistic was measuring my own reconstruction.** The baseline stores a
histogram, not raw samples, so running KS meant reconstructing samples from it. At bin
midpoints, the reconstructed CDF is a ~10-step staircase while production's is smooth,
and KS reports the staircase: **0.30 and 0.38 ("breached") on a control window where
PSI said 0.009 and 0.002 ("stable")**. Spreading uniformly within bins roughly halved
the error and still left a false positive on token counts, which are small integers.

Resolved by dropping KS on reconstructed data. PSI is the correct statistic for binned
reference data; a directional **median shift** was added alongside it, because PSI's
`(a-b)·ln(a/b)` term is symmetric and cannot say *which way* a distribution moved.
`ks_statistic` remains in `src/drift/metrics.py`, tested, for the case where both
sides have real samples.

**2. The baseline measured confidence on training data.** A model is systematically
more confident on documents it memorised: **p10 0.865 on train vs 0.731 held out**.
Comparing production against the training figure reports a 15% "decay" that is really
memorisation — enough to breach the threshold on an unshifted window, permanently,
from day one.

Fixed in M1: the baseline's confidence reference now comes from the **held-out golden
set**, while the input distributions still come from training data (they answer a
different question — "what does normal input look like to this model"). The artifact
records `confidence_source` so a reader can tell which was used, and
`BASELINE_SCHEMA_VERSION` moved to 1.1.0.

## What is missing

- **No scheduled run.** The drift Lambda, its EventBridge schedule, and the S3 report
  destination are not deployed. Every report here was produced by the CLI.
- **No real data-capture input.** These windows are generated documents scored
  locally. `parse_capture_record` handles the SageMaker capture envelope and is
  tested, but has never seen a real capture file.
- **No full retrain → gate → approve → canary cycle.** The retrain state machine is
  written and its safety properties are tested (registration is always
  `PendingManualApproval`; no endpoint API appears anywhere in it), but it has never
  executed. This is the other half of M5's deliverable and it is absent.
- **The override-rate reference is supplied by hand in scenario 3.** M1's baseline
  carries none, because it is built from training data where nothing was reviewed. In
  production it would come from a previously-captured window.

Reproduce with `make drift-scenarios`.
