# M6 evidence — CI runs

**These runs are real and green.** Unlike every other evidence folder in this repo,
nothing here is local or simulated: GitHub Actions executed these on `5fd58c5`.

## PR workflow — success

https://github.com/DGalstyan/mlops-intake-platform/actions/runs/30558325735

| Job | Result |
|---|---|
| Lint, type-check, test | **success** |
| Build inference image | **success** |
| Terraform fmt + validate | **success** |
| Terraform plan (dev) | skipped |
| PR gate | **success** |

## main workflow — success

https://github.com/DGalstyan/mlops-intake-platform/actions/runs/30558325825

| Job | Result |
|---|---|
| Verify | **success** |
| Preflight | **success** |
| Build and push image | skipped |
| Terraform apply (dev) | skipped |
| Integration test | skipped |
| Promote | skipped |

## Why jobs are skipped, and why that is green rather than red

Every skipped job needs AWS. They are gated on `AWS_DEPLOY_ROLE_ARN` being set as a
repository variable, and it is not — no account is configured for this repo.

That gating is deliberate. A PR check that cannot run without cloud access does not
run on forks, and it makes "is this change correct?" depend on "is the account
reachable?". Everything that can be verified without an account is verified without
one: lint, strict type-checking, 5 regression proofs, the full test suite,
`terraform fmt` + `validate` on all three roots, and a container build whose
`/ping` and `/invocations` contract is exercised against the built image.

Setting `AWS_DEPLOY_ROLE_ARN` after a first `make bootstrap && make apply ENV=dev`
turns the remaining jobs on. **They have never run**, so the plan comment, the ECR
push, the apply and the endpoint smoke test are all unproven.

## The regression proof

`scripts/prove_regression_tests.py` injects each defect into a copy of the tree,
asserts the nominated test **fails**, restores, and asserts it passes again. Both
directions matter: a test that fails on everything is as useless as one that fails on
nothing.

| Regression | Caught by | Status |
|---|---|---|
| `inference-contract-rename` | `TestResponseContract` | CAUGHT |
| `asl-retry-jitter-removed` | `test_every_retrier_uses_full_jitter` | CAUGHT |
| `retrain-auto-approves` | `test_registration_is_always_pending_manual_approval` | CAUGHT |
| `drift-baseline-uses-training-confidence` | `test_confidence_reference_is_held_out_not_training` | CAUGHT |
| `idempotency-guard-removed` | `test_review_task_creation_is_conditional` | CAUGHT |

5 of 5 caught.

**They were not all caught on the first run, and that is the point of writing this.**
Three problems surfaced:

1. **A test that silently skipped.** `test_confidence_reference_is_held_out_not_training`
   read a generated artifact and skipped when it was absent — which is always, in CI
   and on a fresh clone. A skipped test looks green in every report, so it could never
   have caught anything. Rewritten to call `build_baseline` directly.
2. **An assertion weak enough to accept a no-op.** The idempotency tests checked that
   the condition expression *contained* `attribute_not_exists`, which
   `attribute_exists(x) or attribute_not_exists(x)` satisfies while guarding nothing.
   Now exact-match.
3. **Two bugs in the harness itself** — `shutil.ignore_patterns` matches by name at
   every level, so excluding `data` also excluded `src/data` and the copy could not
   import its own packages; and replacing only the first occurrence meant the
   injection mutated one guard while the test checked another.

## Three bugs CI caught that local development had masked

The first CI run failed, and every failure was real.

1. **`--require-hashes=false` in the Dockerfile.** It is a boolean flag and takes no
   value, so pip exited 2 before installing anything. Wrong since M2, never caught
   because the image had never been successfully built — docker hung locally and the
   build was abandoned. CI failed on it in 1.1 seconds.
2. **`flask` missing from `requirements-dev.txt`.** `src/inference/serve.py` imports
   it, but it was only declared in `requirements-inference.txt`. mypy passed locally
   purely because flask happened to be installed by hand. This is exactly the failure
   a declared dependency set exists to prevent: a working local environment no clean
   machine can reproduce.
3. **The container could not have started on SageMaker.** `ENTRYPOINT ["gunicorn"]`
   with the arguments in CMD looks right and is not: a command passed to `docker run`
   *replaces* CMD, so SageMaker's `serve` argument became gunicorn's module name. The
   container ran `gunicorn serve` and the worker died with `ModuleNotFoundError`. The
   image builds, the Dockerfile reads correctly, and the endpoint would have failed
   to start. The Dockerfile even carried a comment claiming this was handled.

The third is the argument for **running** the container in CI, not just building it.
It was an M2 bug that would have survived to a live deployment.

Reproduce the proof locally with `make prove-regressions`, and the whole PR check
with `make ci-local`.
