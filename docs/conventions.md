# Project conventions

The rules this repo holds itself to. Read before adding Terraform, Python, ASL or
docs. Every one of these exists because breaking it costs marks in
`docs/ASSIGNMENT.md` §4, or because it bit us once already.

---

## Non-negotiable guardrails

1. **No secrets in the repo.** No account IDs, no `AKIA...` keys, no tokens.
   Account IDs come from `data.aws_caller_identity`; secrets from SSM or Secrets
   Manager references; CI credentials from GitHub OIDC, never long-lived keys.
2. **No wildcard IAM grants.** Never `iam:*`, `Action: "*"`, or `Resource: "*"` —
   not even temporarily. Where AWS genuinely mandates a `Resource: "*"` (there are
   a handful), it is inventoried by category in `docs/decisions.md` and
   `make wildcard-audit` regenerates the file:line list. **Never write down a
   count** — three separate hardcoded counts in this repo went stale.
3. **No console clicks.** Every AWS resource is created by Terraform. If you
   explore by clicking, import it or delete and codify it.
4. **Everything is destroyable.** `make destroy` must leave the account clean.
   `force_destroy` on scratch buckets, no `prevent_destroy`. Anything that
   survives teardown (the KMS key's 7-day window, the state backend) is named
   explicitly in the README.
5. **The README describes what was BUILT, not intentions.** If it is not
   implemented, it does not appear as though it is. This is the single easiest
   mark to lose and we have lost it once already — the README claimed "only M0 is
   implemented" two milestones after that stopped being true.

---

## Repo layout

```
infra/                 Terraform
  bootstrap/           state backend + OIDC provider (local state, applied once)
  modules/             kms, ecr, s3_bucket, iam_role, stack, endpoint, intake, observability
  envs/{dev,staging}/   thin roots over modules/stack, one -var-file each
src/
  config.py           thresholds, seeds, gate margins, schema versions
  data/               synthetic generator
  training/           train, evaluate, metrics, baseline, lineage, registry
  inference/          handlers, serving layer, Dockerfile
  pipeline/           OCR normalisation, validation, prompts, review API
  drift/              drift math + report (M5)
schemas/              one JSON Schema per document class — the source of truth
statemachines/        ASL definitions
scripts/              operational entrypoints (smoke test, seeding, simulation)
config/prices.json    AWS price constants, read by Terraform AND Python
tests/                unit, contract, ASL-structure, drift-guard
evidence/             per-milestone deliverables, each with its own caveat
docs/                 conventions, decisions, runbook, ASSIGNMENT
tasks/                milestone breakdown M0–M7
```

---

## Coding standards

- **Typed and tested.** `mypy --strict` clean, `pytest` green. Both are `make`
  targets and both must pass before a commit.
- **The AI-specific parts are swappable without touching the plumbing.** The
  classifier lives behind a five-method interface (`fit / predict /
  predict_proba / save / load`); the extraction prompt is *data* rendered from
  `schemas/` into DynamoDB. Replacing the model or a prompt must not require
  editing Terraform, ASL, or handlers.
- **Config over hardcoding.** Thresholds, gate margins, price constants and
  environment names live in `src/config.py`, `config/prices.json`, or Terraform
  variables — never inline.
- **One source of truth for anything duplicated.** Prices are read by both
  Terraform and Python from one file. The state bucket name is derived from
  `(project, account)` in three places, each carrying a comment pointing at the
  other two. A constant that must agree in two places will eventually disagree.
- **Pure functions for anything worth testing.** Metric and calibration math take
  arrays and return numbers, with no model, file or AWS dependency, so the math
  can be checked against hand-computed values rather than against a previous run
  of itself.

---

## Naming & tagging

- Resource names: `intake-<component>-<env>` (e.g. `intake-artifacts-dev`).
  S3 buckets append the account id, because bucket names are globally unique —
  documented as an explicit exception.
- Every resource carries `default_tags`: `project`, `environment`, `managed_by`.
  `component` is passed per-resource, because a provider-level default cannot
  vary per resource.
- Metrics namespace: `Intake/Platform`. Correlation id field name:
  `correlation_id`, and it is **never** a metric dimension — that would create one
  custom metric per document.

---

## Testing conventions

Tests earn their place by catching a regression that would otherwise be silent.
The categories that have actually caught bugs here:

- **Contract tests** on anything crossing a boundary. The inference response shape
  is read by three later milestones, so a renamed key is invisible at build time
  and breaks all three at runtime.
- **Structural tests** on the ASL: retry-with-jitter on every fallible state, a
  catch-all last on every Task, conditional writes on both the result and the
  review task. A syntax check would pass a definition that silently drops
  documents on a throttle.
- **Drift guards** between things that must agree: the simulator vs the deployed
  ASL, the ASL's placeholders vs Terraform's substitutions, the alarm inventory vs
  the alarms in `infra/`, the priced model vs the deployed model.
- **Determinism tests** that survive a fresh interpreter. Python randomises string
  hashing per process, so a seed derived from `hash("label")` produces different
  data in every run while looking perfectly deterministic inside one test session.

---

## What the README must contain

- Architecture diagram, with an honest status key.
- `make`-based quickstart from an empty AWS account.
- **Decision log** (`docs/decisions.md`): each entry is "chose X over Y because Z,
  and here's when I'd flip it", including rejected alternatives and
  over-engineering that was deleted.
- **Cost table** with the price constants used and where they came from.
- **Known gaps**: what is broken, what is unverified, and what was not attempted.

---

## Definition of done for a milestone

A milestone is done when its named deliverable exists in `evidence/` **and** a
rubric audit against `docs/ASSIGNMENT.md` §4 finds no instant point-losers.
Code that is written but unverified is not done — and `evidence/` entries state
plainly what was verified against AWS and what was not.
