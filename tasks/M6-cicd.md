# M6 — CI/CD

**Grade tie-in:** cross-cutting; release safety

## Goal
A green PR run and a green main run — with OIDC and **no long-lived AWS keys.**

## Tasks
- [ ] GitHub OIDC federation to AWS; trust policy scoped to this repo +
      branch/environment; least-privilege deploy role (author with iac-terraform).
      No `AKIA...` keys anywhere.
- [ ] PR workflow (`.github/workflows/pr.yml`): lint, type-check, unit tests,
      `terraform validate` + `plan` posted as a PR comment, container build.
- [ ] Main workflow (`.github/workflows/main.yml`): build/push image, `terraform
      apply` to dev, integration test against the live pipeline, promote.
- [ ] Separate manually-triggered retrain workflow (`workflow_dispatch`).
- [ ] At least one **regression-catching test** (inference contract test, ASL
      definition validation, or model-output↔consumer schema-compat test) — prove
      it fails on the regression it targets.
- [ ] Pin action + tool versions; mask sensitive plan outputs.

## Acceptance criteria (Deliverable)
- [ ] Green PR run and green main run captured in `evidence/`.
- [ ] Grep confirms no long-lived keys / secrets / account-ids committed.
- [ ] The regression test demonstrably catches its target regression.

## Definition of done
A rubric audit confirms OIDC-only auth, all PR gates present, a working main
deploy path, and a regression test that earns its place.
