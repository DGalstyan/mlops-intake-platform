# M1 evidence — two distinguishable registry versions

Both scored on the same frozen 240-document golden set, held out of training
and asserted non-overlapping in code (`non_overlap_verified: true`).

| version | difference | macro-F1 | accuracy | ECE | gate |
|---|---|---|---|---|---|
| v1 | calibrated | 0.9417 | 0.9417 | 0.0140 | PASS |
| v2 | calibration disabled | 0.9543 | 0.9542 | 0.2622 | BLOCKED |

## Why these two are the interesting pair

v2 is **more accurate** than v1 (0.9543 vs 0.9417 macro-F1) and its calibration is
**19x worse** (ECE 0.2622 vs 0.0140).

That is the whole argument for reporting calibration next to accuracy. The intake
Route state gates auto-approval on `max(predict_proba)`, so v2 would auto-approve
documents it should have escalated while looking like the better model on every
accuracy-shaped metric. Picking v2 on macro-F1 alone is the mistake this pair exists
to make visible.

## The gate blocked the candidate

```
reason: macro-F1 improvement +0.0126 is below the required +0.0200
required improvement: +0.0200
actual improvement:   +0.0126
```

The margin exists so the gate does not fire on evaluation noise. Here it also
happens to block a model that would have degraded routing — but note that it
blocked on the *margin*, not on calibration. The gate does not currently read ECE;
that is a real gap, recorded in the README.

## Per-class F1 (v1)

| class | precision | recall | F1 | support |
|---|---|---|---|---|
| invoice | 0.9831 | 0.9667 | 0.9748 | 60 |
| medical_report | 0.9333 | 0.9333 | 0.9333 | 60 |
| id_document | 0.9322 | 0.9167 | 0.9244 | 60 |
| correspondence | 0.9194 | 0.9500 | 0.9344 | 60 |

## Confidence distribution (v1)

```
{
  "mean": 0.9324848294760526,
  "p10": 0.7311489146046362,
  "p50": 0.9905776276165221,
  "p90": 0.9975845410628019
}
```

Reproduce with `make two-versions`.
