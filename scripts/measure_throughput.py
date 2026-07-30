#!/usr/bin/env python3
"""Measure in-process inference latency and throughput.

Purpose: give the autoscaling target value in
`infra/modules/endpoint/variables.tf` a measured basis instead of a guess, and
give the rollback latency alarm a threshold anchored to a real p99.

**What this does and does not measure.** It exercises the real handler path
(`input_fn` -> `predict_fn` -> `output_fn`) against the real model, so it captures
the model's own cost honestly. It does *not* include HTTP framing, gunicorn's
scheduling, network latency, or the difference between this machine and an
ml.t3.medium — which is a burstable 2-vCPU instance, while this measurement runs on
a developer laptop with considerably more headroom. Numbers here are therefore an
**upper bound** on per-instance capacity, and the derived target applies an
explicit derating factor rather than pretending otherwise.

A real load test against a deployed endpoint would replace this. That needs AWS
credentials, which is why this exists.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data import generate  # noqa: E402
from src.inference.inference import input_fn, output_fn, predict_fn  # noqa: E402
from src.training.model import TfidfLinearClassifier  # noqa: E402

# How much of the measured laptop capacity to assume an ml.t3.medium delivers.
# t3.medium is 2 burstable vCPUs; this machine has 6 physical cores at a higher
# clock. 0.35 is deliberately pessimistic — the cost of being wrong in this
# direction is one extra instance, while the cost of being wrong the other way is
# a saturated endpoint that scales out only after it is already queueing.
INSTANCE_DERATING_FACTOR = 0.35

# Additional headroom applied to the scaling target. Target-tracking scaling is a
# steady-state signal and scale-out takes minutes to bring an instance into
# service, so the target must sit below capacity or the endpoint saturates before
# help arrives.
SCALING_HEADROOM_FACTOR = 0.60

# Multiple of the measured p99 used for the rollback latency alarm. The alarm's
# job is to catch a variant that is *pathologically* slow, not marginally slower —
# a rollback triggered by ordinary variance is worse than none, because it trains
# people to disable the guardrail. 7x is comfortably above normal jitter and cold
# starts while still firing on a variant that has become unusable. Note the
# measured p99 excludes HTTP framing and SageMaker overhead, so the real p99 will
# be higher and the effective multiple correspondingly smaller.
LATENCY_ALARM_P99_MULTIPLIER = 7


def measure(
    *, requests: int, warmup: int, batch_size: int
) -> dict[str, Any]:
    docs = generate.generate_documents(docs_per_class=60, seed=987)
    model = TfidfLinearClassifier(seed=987, min_df=1)
    model.fit([d.text for d in docs], [d.label for d in docs])

    probe_docs = generate.generate_documents(docs_per_class=40, seed=555)
    texts = [d.text for d in probe_docs]

    def one_request(index: int) -> None:
        start = (index * batch_size) % (len(texts) - batch_size)
        payload = json.dumps({"texts": texts[start : start + batch_size]})
        parsed = input_fn(payload)
        predictions = predict_fn(parsed, model)
        output_fn(predictions)

    for i in range(warmup):
        one_request(i)

    latencies_ms: list[float] = []
    wall_start = time.perf_counter()
    for i in range(requests):
        started = time.perf_counter()
        one_request(i)
        latencies_ms.append((time.perf_counter() - started) * 1000.0)
    wall_seconds = time.perf_counter() - wall_start

    latencies_ms.sort()

    def percentile(p: float) -> float:
        if not latencies_ms:
            return 0.0
        index = min(int(len(latencies_ms) * p / 100.0), len(latencies_ms) - 1)
        return latencies_ms[index]

    measured_rps = requests / wall_seconds
    measured_rpm = measured_rps * 60.0
    instance_rpm = measured_rpm * INSTANCE_DERATING_FACTOR
    target_rpm = instance_rpm * SCALING_HEADROOM_FACTOR

    return {
        "requests": requests,
        "batch_size": batch_size,
        "documents_per_request": batch_size,
        "wall_seconds": round(wall_seconds, 3),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies_ms), 3),
            "p50": round(percentile(50), 3),
            "p95": round(percentile(95), 3),
            "p99": round(percentile(99), 3),
            "max": round(latencies_ms[-1], 3),
        },
        "measured_requests_per_second": round(measured_rps, 1),
        "measured_invocations_per_minute": round(measured_rpm),
        "derating_factor": INSTANCE_DERATING_FACTOR,
        "estimated_instance_invocations_per_minute": round(instance_rpm),
        "scaling_headroom_factor": SCALING_HEADROOM_FACTOR,
        "recommended_autoscaling_target": round(target_rpm / 50) * 50,
        "latency_alarm_p99_multiplier": LATENCY_ALARM_P99_MULTIPLIER,
        "recommended_latency_alarm_ms": max(
            500,
            round(percentile(99) * LATENCY_ALARM_P99_MULTIPLIER / 100) * 100,
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    result = measure(
        requests=args.requests, warmup=args.warmup, batch_size=args.batch_size
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
