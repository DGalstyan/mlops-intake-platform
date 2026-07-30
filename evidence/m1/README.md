# M1 evidence — training and the registry

## What is real

Genuine training output. `make two-versions` reproduces every number here from the
seed in `src/config.py`.

| version | difference | macro-F1 | ECE | gate |
|---|---|---|---|---|
| v1 | calibrated | 0.9417 | 0.0140 | pass |
| v2 | calibration disabled | **0.9543** | **0.2622** | blocked |

**v2 is more accurate and 19× worse calibrated.** Since routing gates on
`max(predict_proba)`, v2 would confidently auto-approve documents it should escalate
while winning on every accuracy-shaped metric. That pair is the argument for reporting
ECE next to F1, and it is why the two registry versions differ by calibration rather
than by a random seed.

The gate blocked v2 — but on the macro-F1 **margin**, not on calibration. The gate
does not read ECE. A candidate improving macro-F1 by 0.03 while wrecking ECE would
pass. That is a real gap, recorded in the README.

## Files

- `two-versions.md` — the comparison and the gate decision
- `v1-golden-metrics.json`, `v2-golden-metrics.json` — held-out metrics, `split: golden`
- `v1-baseline-statistics.json` — the M5 contract. Note `confidence_source:
  golden_holdout`: the confidence reference is measured on held-out data, because a
  model is systematically more confident on documents it memorised and a training-set
  reference makes every production window look decayed.
- `v1-lineage.json` — snapshot id, git SHA, resolved dependency versions
- `v1-registration-request.json` — the exact `create_model_package` payload, `--dry-run`
- `snapshot.json` — the content-addressed data snapshot id

## What is NOT real

- **No version was ever written to a Model Package Group.** `register.py` has only
  been run `--dry-run`. The two-versions deliverable is proven as far as metrics and
  the registration payload; the API call itself is unverified.
- **Nothing ran on SageMaker.** `train.py` and `evaluate.py` implement the script-mode
  and Processing-job contracts and run correctly locally, but no job was submitted and
  no `model.tar.gz` exists in S3.
- `training_image_digest` is `unknown` in the lineage, correctly — there was no
  container to take a digest from.
- The golden set is synthetic and drawn from the same generator as training. It is
  genuinely held out and non-overlap is asserted in code, but it is not an independent
  sample of the world, so the absolute scores mean little. The *relative* comparison
  between versions is what the gate uses, and that remains valid.
