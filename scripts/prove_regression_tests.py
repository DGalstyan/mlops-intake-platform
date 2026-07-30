#!/usr/bin/env python3
"""Prove the regression tests actually catch the regressions they target.

M6 asks for "at least one test that would have caught a real regression" and to
*prove* it fails on that regression. A test that has only ever been seen passing is
an assertion about nothing — it might be tautological, or asserting on the wrong
object, or silently skipped.

This injects each regression into a copy of the repo, runs the nominated test, and
checks it FAILS. Then it restores and checks the test passes again. Both directions
matter: a test that fails on everything is as useless as one that fails on nothing.

Every regression below is one that actually happened in this repo, or one that would
be invisible in review.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Regression:
    """One injectable defect and the test that must catch it."""

    name: str
    why_it_matters: str
    target_file: str
    find: str
    replace: str
    test: str


REGRESSIONS: tuple[Regression, ...] = (
    Regression(
        name="inference-contract-rename",
        why_it_matters=(
            "Renaming a response key is invisible at build time. The endpoint keeps "
            "returning 200 and every CloudWatch metric stays green, while M3's Route "
            "state, M4's metrics and M5's drift parsing all break at runtime."
        ),
        target_file="src/inference/inference.py",
        find='"confidence": confidence,',
        replace='"score": confidence,',
        test="tests/test_inference.py::TestResponseContract",
    ),
    Regression(
        name="asl-retry-jitter-removed",
        why_it_matters=(
            "Without full jitter, concurrent executions retry in lockstep. A batch "
            "S3 upload starts many at once, they re-throttle each other, and "
            "documents are lost to a throttle the retry policy was supposed to "
            "absorb. Nothing about the definition looks wrong."
        ),
        target_file="statemachines/intake.asl.json",
        find='"JitterStrategy": "FULL"',
        replace='"JitterStrategy": "NONE"',
        test="tests/test_asl.py::TestRetryPolicies::test_every_retrier_uses_full_jitter",
    ),
    Regression(
        name="retrain-auto-approves",
        why_it_matters=(
            "The single most dangerous change possible in this repo: a retrained "
            "model registering itself as Approved deploys itself, with no human "
            "between 'the numbers improved' and 'serving production traffic'. It is "
            "a one-word diff."
        ),
        target_file="statemachines/retrain.asl.json",
        find='"ModelApprovalStatus": "PendingManualApproval"',
        replace='"ModelApprovalStatus": "Approved"',
        test="tests/test_asl.py::TestRetrainSafety::test_registration_is_always_pending_manual_approval",
    ),
    Regression(
        name="drift-baseline-uses-training-confidence",
        why_it_matters=(
            "A model is more confident on documents it memorised. Sourcing the "
            "baseline's confidence reference from training data makes every "
            "production window report ~15% decay that is really memorisation — a "
            "permanent false alarm from day one, which gets the detector muted."
        ),
        target_file="src/training/baseline.py",
        find='"confidence_source": "golden_holdout" if holdout_confidences else "train",',
        replace='"confidence_source": "train",',
        test="tests/test_drift.py::TestFalsePositiveRegressions::test_confidence_reference_is_held_out_not_training",
    ),
    Regression(
        name="metrics-dimension-mismatch",
        why_it_matters=(
            "CloudWatch treats the dimension SET as part of a metric's identity and "
            "does not roll up. Emitting [Environment, DocumentClass] while the alarms "
            "query [Environment] leaves 6 of 11 alarms in INSUFFICIENT_DATA forever — "
            "and with treat_missing_data=notBreaching they never fire, while looking "
            "perfectly configured. This was live in the repo; two tests passed "
            "throughout because they compared metric NAMES only."
        ),
        target_file="statemachines/intake.asl.json",
        find='"Dimensions": [\n              {\n                "Name": "Environment",\n                "Value": "${Environment}"\n              }\n            ]',
        replace='"Dimensions": [\n              {\n                "Name": "Environment",\n                "Value": "${Environment}"\n              },\n              {\n                "Name": "DocumentClass",\n                "Value.$": "$.classification.predicted_class"\n              }\n            ]',
        test="tests/test_observability.py::TestMetricEmission::test_every_consumed_metric_is_emitted_with_the_dimensions_it_is_queried_by",
    ),
    Regression(
        name="idempotency-guard-removed",
        why_it_matters=(
            "Dropping the conditional write on the review task means one document "
            "can produce two review tasks — wasting a reviewer's time and producing "
            "two conflicting corrections that both become training labels."
        ),
        target_file="statemachines/intake.asl.json",
        find='"ConditionExpression": "attribute_not_exists(correlation_id)"',
        replace='"ConditionExpression": "attribute_exists(correlation_id) or attribute_not_exists(correlation_id)"',
        test="tests/test_asl.py::TestIdempotency::test_review_task_creation_is_conditional",
    ),
)


def run_test(workdir: Path, test: str) -> tuple[bool, str]:
    """Run one test selector. Returns (passed, tail of output)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "--no-header", "-x"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=900,
    )
    output = (result.stdout + result.stderr).strip().splitlines()
    return result.returncode == 0, "\n".join(output[-4:])


def prove(regression: Regression, workdir: Path) -> dict[str, object]:
    target = workdir / regression.target_file
    original = target.read_text(encoding="utf-8")

    if regression.find not in original:
        return {
            "regression": regression.name,
            "status": "ERROR",
            "detail": (
                f"the pattern to inject was not found in {regression.target_file}. "
                "The code changed and this proof is stale."
            ),
        }

    # 1. baseline: the test must pass on clean code.
    passes_clean, clean_output = run_test(workdir, regression.test)

    # 2. inject the regression; the test must now FAIL.
    # Replace EVERY occurrence, not just the first. An earlier version replaced only
    # the first, so injecting "the idempotency guard was removed" mutated the result
    # guard while the nominated test checked the review-task guard — the injection and
    # the assertion were looking at different code, and the proof reported a false
    # "not caught".
    target.write_text(original.replace(regression.find, regression.replace), encoding="utf-8")
    passes_broken, broken_output = run_test(workdir, regression.test)

    # 3. restore.
    target.write_text(original, encoding="utf-8")

    caught = passes_clean and not passes_broken
    return {
        "regression": regression.name,
        "test": regression.test,
        "why_it_matters": regression.why_it_matters,
        "passes_on_clean_code": passes_clean,
        "fails_on_injected_regression": not passes_broken,
        "status": "CAUGHT" if caught else "NOT CAUGHT",
        "detail": "" if caught else f"clean: {clean_output}\nbroken: {broken_output}",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--only", default=None, help="Prove a single regression by name."
    )
    args = parser.parse_args(argv)

    selected = [r for r in REGRESSIONS if args.only in (None, r.name)]
    if not selected:
        print(f"no regression named {args.only!r}", file=sys.stderr)
        return 1

    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "repo"
        # Copy rather than mutate the real tree: an interrupted run must not leave an
        # injected defect behind in the working copy.
        # Anchored to the repo root. shutil.ignore_patterns matches by NAME at every
        # level, so a bare "data" pattern also excluded src/data and the copied tree
        # could not import its own packages. Only the top-level generated
        # directories are skipped.
        top_level_generated = {"build", "data", "artifacts", "evidence"}
        anywhere = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".terraform"}

        def ignore(directory: str, entries: list[str]) -> set[str]:
            skipped = {e for e in entries if e in anywhere}
            if Path(directory).resolve() == REPO.resolve():
                skipped |= {e for e in entries if e in top_level_generated}
            return skipped

        shutil.copytree(REPO, workdir, ignore=ignore)
        for regression in selected:
            print(f"proving {regression.name} ...", file=sys.stderr)
            results.append(prove(regression, workdir))

    caught = sum(1 for r in results if r["status"] == "CAUGHT")
    summary = {
        "proved": len(results),
        "caught": caught,
        "results": results,
    }

    text = json.dumps(summary, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")

    if caught != len(results):
        print(
            f"\n{len(results) - caught} regression(s) were NOT caught by their "
            "nominated test.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
