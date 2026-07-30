# M0 — Foundations (IaC bootstrap)

**Owner:** `iac-terraform`  ·  **Skills:** `terraform-aws-conventions`,
`mlops-project-conventions`  ·  **Grade tie-in:** IaC & reproducibility (15%)

## Goal
`terraform plan` on a clean account produces the whole stack, with no manual step
beyond credentials.

## Tasks
- [ ] Bootstrap remote state: S3 backend + DynamoDB lock table (or S3 native
      locking) via a separate `make bootstrap` target (local state, applied once).
- [ ] Environment separation for `dev` and `staging` via workspaces or
      `-var-file`. No hardcoded environment strings.
- [ ] ECR repository (immutable tags, scan-on-push).
- [ ] S3 buckets: raw, processed, artifacts, data-capture — each with versioning,
      SSE-KMS, public-access-block, and lifecycle rules.
- [ ] One KMS key with an explicit, least-privilege key policy + rotation.
- [ ] IAM roles scoped **per component** (training, endpoint, state machine, each
      Lambda, CI deploy). No `iam:*`, no `Resource: "*"`.
- [ ] `default_tags` on everything (project, environment, component, managed_by).
- [ ] Makefile: `bootstrap`, `plan`, `apply`, `destroy`.

## Acceptance criteria (Deliverable)
- [ ] `terraform validate` passes and `terraform fmt -recursive` is clean.
- [ ] `terraform plan` on a clean account renders the full stack; no manual steps.
- [ ] Grep confirms zero wildcards and zero secrets/account-ids in the repo.
- [ ] `make destroy` verified to leave the account clean (note any bootstrap
      resource torn down separately).
- [ ] Decision-log entries drafted for: state-locking choice, KMS granularity,
      bucket lifecycle windows (each with the rejected alternative).

## Definition of done
`mlops-reviewer` finds no instant point-losers in the IaC and confirms the plan
builds the whole stack.
