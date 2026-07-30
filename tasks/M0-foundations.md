# M0 — Foundations (IaC bootstrap)

**Owner:** `iac-terraform`  ·  **Skills:** `terraform-aws-conventions`,
`mlops-project-conventions`  ·  **Grade tie-in:** IaC & reproducibility (15%)

**Status: NOT DONE — one blocker left.** A1 (produce the deliverable) is the only
open item, and it needs AWS credentials. Findings A2–A17 from the audit are
fixed. See "Audit status" below. Audited by `mlops-reviewer` 2026-07-30; fixes
applied same day.

## Goal
`terraform plan` on a clean account produces the whole stack, with no manual step
beyond credentials.

## Tasks
- [x] Bootstrap remote state: S3 backend + DynamoDB lock table (or S3 native
      locking) via a separate `make bootstrap` target (local state, applied once).
      → `infra/bootstrap/`. Chose S3 native locking (`use_lockfile`), no DynamoDB;
      `required_version >= 1.10.0` declared in all four roots. **Never applied.**
- [x] Environment separation for `dev` and `staging` via workspaces or
      `-var-file`. No hardcoded environment strings.
      → `-var-file` + thin roots over `infra/modules/stack`. Makefile guard and a
      module `validation` block. `dev`/`staging` variables are byte-identical.
- [x] ECR repository (immutable tags, scan-on-push).
      → `infra/modules/ecr`: `IMMUTABLE` + `scan_on_push` + `force_delete`.
- [x] S3 buckets: raw, processed, artifacts, data-capture — each with versioning,
      SSE-KMS, public-access-block, and lifecycle rules.
      → `infra/modules/s3_bucket` ×4. Also `bucket_key_enabled`, noncurrent-version
      expiry, multipart abort, and `force_destroy`.
- [x] One KMS key with an explicit, least-privilege key policy + rotation.
      → `infra/modules/kms`: enumerated actions (no `kms:*`), rotation enabled,
      admin principal an explicit opt-in variable with an empty default.
- [x] IAM roles scoped **per component** (training, endpoint, state machine, each
      Lambda, CI deploy). No `iam:*`, no `Resource: "*"`.
      → training / endpoint / state-machine / ci-deploy exist, each with
      `aws:SourceAccount` + scoped `aws:SourceArn` on its service trust policy.
      Lambda roles deliberately deferred to the milestone that creates each
      function (recorded as a decision, not a gap). Zero `iam:*`; six
      `Resource: "*"` sites remain, all inventoried in `docs/decisions.md`.
- [x] `default_tags` on everything (project, environment, component, managed_by).
      → Env roots set project / environment / managed_by; `component` is passed
      per-resource because a provider-level default cannot vary per resource.
      Documented as a decision.
- [x] Makefile: `bootstrap`, `plan`, `apply`, `destroy`.
      → Plus `destroy-bootstrap`, `init`, `fmt`, `validate`, `test`.

## Acceptance criteria (Deliverable)
- [x] `terraform validate` passes and `terraform fmt -recursive` is clean.
      → Verified on all three roots. Terraform 1.15.8, AWS provider 5.100.0.
- [ ] `terraform plan` on a clean account renders the full stack; no manual steps.
      → **BLOCKED: no AWS credentials configured.** This is the named deliverable.
- [x] Grep confirms zero wildcards and zero secrets/account-ids in the repo.
      → No `AKIA`/`ASIA`, no real account ids, no `iam:*`, no service-level action
      wildcards. Six `Resource: "*"` sites remain — all AWS-mandated, now
      inventoried with file:line and the restriction behind each in
      `docs/decisions.md`, plus one Deny-on-insecure-transport bucket policy.
- [ ] `make destroy` verified to leave the account clean (note any bootstrap
      resource torn down separately).
      → **BLOCKED: never applied.** Fix A10 and A11 *before* capturing this as
      evidence, or the transcript will show a leftover KMS key.
- [x] Decision-log entries drafted for: state-locking choice, KMS granularity,
      bucket lifecycle windows (each with the rejected alternative).
      → `docs/decisions.md`, now 13 entries, all in the required format. Added:
      the wildcard inventory, deleted over-engineering, `force_destroy` vs
      `prevent_destroy`, the KMS deletion window, the OIDC singleton, the derived
      state-bucket name, and per-resource `component` tagging.

## Definition of done
`mlops-reviewer` finds no instant point-losers in the IaC and confirms the plan
builds the whole stack.

---

## Audit status — `mlops-reviewer`, 2026-07-30

Original verdict: **NOT DONE** — `evidence/` empty plus four instant
point-losers. The Terraform itself was judged above the bar for the 15% IaC
weight; what was missing was proof and honest framing.

**A2–A17 are now fixed.** A1 remains, blocked on AWS credentials.

### Instant point-losers
- [ ] **A1 — `evidence/` is empty.** The named deliverable has never been
      produced. **Blocked on AWS credentials — the one thing still standing
      between M0 and done.**
- [x] **A2 — README denied the repo contained an implementation.** Rewritten as
      the platform README: architecture with a built/not-built key, quickstart,
      cost table with price constants, teardown order, honest known-gaps. The
      asset-pack text moved to `docs/asset-pack.md` with a pointer at the top.
- [x] **A3 — False comment in `kms/main.tf`** claiming no component role used
      `Resource: "*"` while inviting the grader to grep. Replaced with an accurate
      six-site count pointing at the decision log.
- [x] **A4 — Wildcard inventory** added to `docs/decisions.md` as a table with
      file:line and the AWS restriction behind each, plus the rejected
      alternative (splitting `aws_kms_key_policy` out) and why.

### Should fix
- [x] **A5** `aws:SourceAccount` + scoped `aws:SourceArn` added to all three
      service trust policies (training, endpoint, state-machine).
- [x] **A6** `make init` now derives the state bucket from
      `aws sts get-caller-identity` instead of reading gitignored local state, so
      it works on a fresh clone and on CI. Added `fmt-check` and `validate-all`
      targets for the PR workflow.
- [x] **A7** `state_machine_permissions` deleted; role is genuinely trust-only
      (`inline_policy_json = null`). Wildcard count 7 → 6. Recorded as the
      "deleted over-engineering" decision entry.
- [x] **A8** OIDC trust narrowed from `repo:<org>/<repo>:*` to `refs/heads/main`
      + `pull_request`.
- [x] **A9** `s3:ListBucket` now also allows the `env:/*` prefix Terraform's S3
      backend lists during `init`. Still unvalidated against a live backend —
      carried into the README's unvalidated-risks list.
- [x] **A10** Added `kms:RetireGrant`, `kms:ListRetirableGrants`,
      `kms:UpdateKeyDescription`, `kms:ReEncryptFrom/To` to the key-admin actions.
- [x] **A11** The 7-day window is now stated in the Makefile comment, echoed in
      `make destroy` output, and documented in the README teardown section and a
      decision entry.
- [x] **A12** `create_github_oidc_provider` toggle added (default `true`), with
      the `EntityAlreadyExists` failure mode explained in the quickstart.
- [x] **A13** The stack resolves the OIDC provider with a `data` source instead of
      a constructed ARN, so a missing bootstrap fails with an accurate message.
- [x] **A14** State bucket name derived from `${var.project}-tfstate-<account>` in
      a single `local` per root, with cross-references between the three
      definitions.
- [x] **A15** Bootstrap state bucket got a lifecycle rule (90-day noncurrent
      expiry — deliberately longer than the data buckets' 30) plus a
      Deny-on-insecure-transport bucket policy.
- [x] **A16** Decision log restructured to 13 entries, all in the required
      format. The two prose entries became real decisions with rejected
      alternatives.
- [x] **A17** Per-resource `component` tagging documented as a decision: a
      provider-level default cannot vary per resource.

### Also fixed (forward-looking, would have failed at M1/M2)
- [x] `kms:CreateGrant` added to the training and endpoint roles, conditioned on
      `kms:GrantIsForAWSResource`. **Without this the first M1 training job fails
      with a KMS AccessDenied before running any Python.** Kept in its own
      statement because that condition key only exists in the grant APIs'
      request context — attaching it to the `Decrypt`/`Encrypt` statement would
      have silently denied them.
- [x] `s3:GetBucketLocation` and the multipart-upload actions
      (`AbortMultipartUpload`, `ListMultipartUploadParts`) added for
      `model.tar.gz` upload retries.
- [x] ECR lifecycle gained a keep-last-10 rule; with immutable tags, tagged
      images otherwise accumulate forever.

### Still open / deferred
- **A1** — needs credentials. Fix A10 and A11 were completed first precisely so
  the destroy transcript is clean when captured.
- `ecr:GetAuthorizationToken` on the training/endpoint roles is probably
  unnecessary (SageMaker pulls same-account images with service credentials).
  Removing it would drop 6 wildcards to 4. Verify against a real training job at
  M1 before deleting.
- Lambda roles, and the ci-deploy role's inability to `terraform apply` — both
  now recorded as decisions with rejected alternatives rather than bare gaps.
- `docs/runbook.md` does not exist yet (M7).

### Next step
`make bootstrap && make plan ENV=dev` against a real account, saving output to
`evidence/m0-plan-dev.txt`; then `apply`, then `destroy` with a
`resourcegroupstaggingapi` listing before and after into
`evidence/m0-destroy-clean.txt`.
