# M0 — Foundations (IaC bootstrap)

**Owner:** `iac-terraform`  ·  **Skills:** `terraform-aws-conventions`,
`mlops-project-conventions`  ·  **Grade tie-in:** IaC & reproducibility (15%)

**Status: NOT DONE** — the code is written and statically validated, but the
milestone's named deliverable is unevidenced and four instant point-losers are
open. See "Audit status" below. Last audited by `mlops-reviewer` 2026-07-30.

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
- [ ] IAM roles scoped **per component** (training, endpoint, state machine, each
      Lambda, CI deploy). No `iam:*`, no `Resource: "*"`.
      → **PARTIAL.** training / endpoint / state-machine / ci-deploy exist; no
      Lambda roles (deferred — no Lambda exists yet). Zero `iam:*`, but seven
      `Resource: "*"` sites remain — see A4.
- [ ] `default_tags` on everything (project, environment, component, managed_by).
      → **PARTIAL.** Env roots set project / environment / managed_by; `component`
      is passed per-resource instead, since a provider-level default cannot vary
      per resource. Needs documenting rather than fixing — see A17.
- [x] Makefile: `bootstrap`, `plan`, `apply`, `destroy`.
      → Plus `destroy-bootstrap`, `init`, `fmt`, `validate`, `test`.

## Acceptance criteria (Deliverable)
- [x] `terraform validate` passes and `terraform fmt -recursive` is clean.
      → Verified on all three roots. Terraform 1.15.8, AWS provider 5.100.0.
- [ ] `terraform plan` on a clean account renders the full stack; no manual steps.
      → **BLOCKED: no AWS credentials configured.** This is the named deliverable.
- [x] Grep confirms zero wildcards and zero secrets/account-ids in the repo.
      → No `AKIA`/`ASIA`, no real account ids, no `iam:*`, no service-level action
      wildcards. **Caveat:** seven `Resource: "*"` sites exist, justified only in
      code comments — see A4.
- [ ] `make destroy` verified to leave the account clean (note any bootstrap
      resource torn down separately).
      → **BLOCKED: never applied.** Fix A10 and A11 *before* capturing this as
      evidence, or the transcript will show a leftover KMS key.
- [x] Decision-log entries drafted for: state-locking choice, KMS granularity,
      bucket lifecycle windows (each with the rejected alternative).
      → `docs/decisions.md`. Four of six entries hit the required format; two are
      prose without a rejected alternative — see A16.

## Definition of done
`mlops-reviewer` finds no instant point-losers in the IaC and confirms the plan
builds the whole stack.

---

## Audit status — `mlops-reviewer`, 2026-07-30

Verdict: **NOT DONE.** Both halves of the done-gate fail: `evidence/` is empty and
four instant point-losers are open. The Terraform itself was judged above the bar
for the 15% IaC weight — what is missing is proof and honest framing, not
engineering.

### Instant point-losers (must clear before M0 is done)
- [ ] **A1 — `evidence/` is empty.** The named deliverable has never been
      produced. Blocked on AWS credentials.
- [ ] **A2 — `README.md` denies the repo contains an implementation.** It still
      describes the asset pack and asserts the repo "contains no AWS resources …
      nothing here spends money or needs teardown". False since M0 landed, and it
      is the first file rendered on a public repo. Rewrite as the platform README;
      move the pack description to `docs/asset-pack.md`.
- [ ] **A3 — False comment at `infra/modules/kms/main.tf:105-106`.** Claims no
      component role uses `Resource: "*"` and invites the grader to grep and
      confirm. Four identity policies do. Replace with an accurate inventory.
- [ ] **A4 — Seven `Resource: "*"` sites justified nowhere a grader reads.**
      `kms/main.tf:52,72,92` + `stack/iam.tf:87,182,248,313`. Each is genuinely
      unavoidable (key-policy self-reference; `ecr:GetAuthorizationToken`;
      CloudWatch Logs delivery), but `docs/decisions.md` never mentions them. Add
      an entry naming file:line and the AWS restriction for each.

### Should fix — costs real points
- [ ] **A5** No `aws:SourceAccount`/`aws:SourceArn` on any service trust policy
      (`stack/iam.tf:20-29`, `:119-128`, `:217-226`) — confused-deputy exposure.
- [ ] **A6** `make init` is unusable without the local bootstrap state, so the CI
      path M6 needs does not exist (`Makefile:38`). Derive the bucket name instead.
- [ ] **A7** State-machine role is documented "trust-only" but attaches a policy
      granting log-delivery on `Resource: "*"` to a role with nothing to run.
      Delete it and re-add in M3. Doubles as the "deleted my own
      over-engineering" decision-log entry the rubric explicitly rewards.
- [ ] **A8** ci-deploy OIDC trust accepts **any** ref on a public repo
      (`stack/iam.tf:303`) and can delete Terraform state. Narrow to `main` +
      `pull_request` now.
- [ ] **A9** The `s3:ListBucket` prefix condition likely blocks `terraform init`,
      which lists with prefix `env:/` (`stack/iam.tf:331-341`). Unvalidated.
- [ ] **A10** KMS admin action list omits `kms:RetireGrant` — a likely **hard
      destroy blocker** for the CMK-encrypted ECR repo. Also missing
      `ListRetirableGrants`, `UpdateKeyDescription`, `ReEncrypt*`.
- [ ] **A11** The 7-day KMS deletion window survives `make destroy` and keeps
      billing; not named in the teardown docs (`kms/main.tf:109`).
- [ ] **A12** The GitHub OIDC provider is an account singleton — `make bootstrap`
      fails with `EntityAlreadyExists` if the reviewer's account already has one.
      Add a `create_github_oidc_provider` toggle.
- [ ] **A13** ci-deploy trusts a *string-constructed* OIDC ARN, so a missing
      bootstrap fails with `MalformedPolicyDocument` naming neither cause. Use a
      `data` source.
- [ ] **A14** State bucket name is a duplicated literal across two roots and
      ignores `var.project` (`bootstrap/main.tf:25`, `stack/main.tf:25`).
- [ ] **A15** Bootstrap state bucket has versioning but no lifecycle rule.
- [ ] **A16** Two of six decision-log entries are prose, not decisions. Missing
      entirely: the wildcard inventory, deleted over-engineering, and why
      `force_destroy` over `prevent_destroy`.
- [ ] **A17** `component` absent from env-root `default_tags` — presentational,
      but a grader reading the checkbox marks it unmet. Document the reason.

### Deferrals judged acceptable
- No Lambda roles yet — a role scoped to a nonexistent function is theatre.
  Reframe as a decision with its rejected alternative.
- ci-deploy cannot yet `terraform apply` the stack's resources — M6 closes this.

### Forward-looking (will fail at M1/M2, not M0)
- Training/endpoint roles lack `kms:CreateGrant` — **will fail the first SageMaker
  training job** against a CMK. Also missing `s3:GetBucketLocation` and the
  multipart-upload actions on the artifacts bucket.
- `ecr:GetAuthorizationToken` on the training/endpoint roles is probably
  unnecessary (SageMaker pulls same-account images with service credentials).
  Removing it would drop two of the seven wildcard sites. Verify at M1.

### Recommended order
A2 → A7 → A3 + A4 → A5 + A8 → A6 → **then** A10 + A11 (before any destroy you
intend to use as evidence) → apply, capture the plan and destroy transcripts → A1.
