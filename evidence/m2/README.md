# M2 evidence — deployment

## What is here

`throughput.json` / `throughput.md` — a real load measurement of the inference path
(`input_fn` → `predict_fn` → `output_fn`) against a real trained model.

| metric | value |
|---|---|
| p50 / p95 / p99 latency | 87 / 166 / 220 ms |
| throughput | 10.8 req/s (649 invocations/min) |

**This measurement corrected two of my own guesses.** Before running it the module
defaulted to an autoscaling target of 900 invocations/min and a latency alarm of
2000 ms. Real per-instance capacity is ~227/min after derating for an `ml.t3.medium`'s
burstable vCPUs — so **900 was roughly 4× capacity** and the policy would effectively
never have scaled out. "Autoscaling on a justified metric" would have been decorative.
Now 150 (measured, derated, with 60% headroom) and 1500 ms (7× measured p99).

## What the measurement does NOT cover

- No HTTP framing, no gunicorn scheduling, no network, no SageMaker routing. Real
  endpoint p99 will be **higher** than 220 ms.
- Ran on a 6-core developer laptop, not on a burstable 2-vCPU `ml.t3.medium`. It is an
  **upper bound** on per-instance capacity, and the 0.35 derating factor is a guess.
- Sequential and single-process, so it says nothing about queueing under concurrency —
  which is exactly what the autoscaling policy reacts to.

## What is ABSENT — M2's actual deliverable

**The recorded bad deploy that rolled back on its own does not exist.** No endpoint has
been deployed. The canary policy and both rollback alarms are written and validated in
Terraform; whether they fire is unproven.

An audit also found that the canary path was **structurally unreachable** as originally
written: `aws_sagemaker_model` and the endpoint config had static names with
`create_before_destroy`, so every endpoint *update* — the only path a canary runs on —
would have failed with "cannot create already existing". Fixed by letting the provider
generate unique names. That defect would have blocked the deliverable even with
credentials.

## What IS verified about the container

The `/ping` and `/invocations` contract is verified by 41 tests against the Flask app
and, in CI, against the **built image running the way SageMaker starts it**. That
caught a bug local review missed: `ENTRYPOINT ["gunicorn"]` meant SageMaker's `serve`
argument became gunicorn's module name, and the container could not have started.
See `evidence/m6/README.md`.
