# M2 evidence — load measurement and derived thresholds

**Status: this is the only M2 evidence that exists.** The headline M2 deliverable —
a recorded bad deploy that rolled back on its own — requires a live SageMaker
endpoint and is **not present**. No AWS credentials are configured. See "What is
missing" below.

## What was measured

`scripts/measure_throughput.py` drives the real handler path
(`input_fn` -> `predict_fn` -> `output_fn`) against a real trained model,
1200 requests at batch size 1 after 100 warmup requests.

| metric | value |
|---|---|
| mean latency | 92.4 ms |
| p50 | 86.6 ms |
| p95 | 165.9 ms |
| p99 | 219.5 ms |
| max | 343.1 ms |
| throughput | 10.8 req/s (649 invocations/min) |

## What it does NOT measure

Being explicit, because the numbers are otherwise easy to over-read:

- No HTTP framing, no gunicorn scheduling, no network, no SageMaker routing
  overhead. Real endpoint p99 will be **higher** than 220 ms.
- This ran on a developer laptop (6-core i7), not on an `ml.t3.medium`, which is a
  **burstable 2-vCPU** instance. Measured throughput is an upper bound on
  per-instance capacity.
- Single-process, sequential. No concurrency, so it says nothing about queueing
  behaviour under parallel load — which is exactly what the autoscaling policy
  reacts to.

A real load test against a deployed endpoint replaces this. That needs credentials.

## Thresholds derived from it

| Setting | Value | Derivation |
|---|---|---|
| `autoscaling_target_invocations_per_instance` | **150** /min | 649/min measured, x0.35 derating for t3.medium = 227/min capacity, x0.6 headroom |
| `rollback_latency_threshold_ms` | **1500** ms | 7x measured p99 of 220 ms |

**Why the derating and headroom factors exist.** The derating (0.35) is
deliberately pessimistic: being wrong in that direction costs one extra instance,
while being wrong the other way means a saturated endpoint that scales out only
after it is already queueing. The headroom (0.6) exists because
target tracking is a *steady-state* signal and bringing an instance into service
takes minutes — a target set at capacity guarantees the endpoint is already behind
by the time scaling reacts.

**This measurement corrected two guesses.** Before running it, the module defaulted
to a target of 900 invocations/min and a latency alarm of 2000 ms. 900 is roughly
**4x** real per-instance capacity — the policy would effectively never have scaled
out, and "autoscaling on a justified metric" would have been decorative. Both
defaults are now the measured values.

## Why the metric is invocations-per-instance and not CPU

CPU utilisation on a sparse dot product barely moves under load: the endpoint
queues before it saturates a core. A CPU-target policy would scale far too late,
or never. `SageMakerVariantInvocationsPerInstance` is the signal that actually
correlates with queueing for this model.

## What is missing

- **The bad-deploy rollback recording.** M2's stated deliverable. Needs a live
  endpoint: deploy a deliberately broken model version, watch the 5xx alarm trip
  during the canary step, capture the CloudWatch alarm state change and the
  endpoint's deployment history showing the automatic revert.
- **A real p99 from CloudWatch `ModelLatency`**, to replace the in-process figure
  above.
- **A concurrent load test**, to validate the autoscaling target against actual
  queueing rather than sequential throughput.
- **Container image verification.** `docker build` was attempted locally and hung;
  the daemon became unresponsive and the build was abandoned. The image is
  therefore **unbuilt and unverified**. The `/ping` and `/invocations` contract *is*
  verified, by 41 tests against the Flask app directly (`tests/test_inference.py`),
  including readiness returning 503 for a model that loads but cannot predict.

Reproduce the measurement with `make measure-throughput`.
