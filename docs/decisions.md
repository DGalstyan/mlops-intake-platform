# Decision log — M0 (Foundations / IaC bootstrap)

Draft entries for the M0 milestone. To be folded into the README's decision
log at M7, alongside entries from later milestones. Format: "I chose X over Y
because Z, and here's when I'd flip that decision."

## State locking: S3 native locking, not a DynamoDB lock table

**Chose:** S3 native locking (`use_lockfile = true` on the `s3` backend
block, Terraform >= 1.10), which stores a lock object alongside the state
file in the same bucket.

**Over:** a DynamoDB lock table (`aws_dynamodb_table` with a `LockID` hash
key), the traditional approach for S3 backends before Terraform 1.10.

**Because:** the DynamoDB table is a second piece of infrastructure that
exists purely to support the first (state locking), with its own IAM
permissions to scope, its own bootstrap/destroy step, and its own (small but
nonzero) cost. S3 native locking gets the same mutual-exclusion guarantee
with one resource instead of two, which matters directly for the "destroy
leaves nothing behind" bar this milestone is graded on — one less thing that
can be left dangling or misconfigured.

**I'd flip this if:** the CI runner or any teammate's local Terraform were
pinned below 1.10 (native locking silently isn't available and `init` fails
outright, not a soft-degrade), or if we needed lock introspection/manual
lock-breaking tooling beyond `terraform force-unlock` — DynamoDB's
`aws dynamodb get-item`/console view is more discoverable for someone
debugging a stuck lock at 3am than an S3 object with no dedicated UI.

## KMS granularity: one key per environment, not per bucket or account-wide

**Chose:** one customer-managed KMS key per environment (`intake-dev`,
`intake-staging`), shared by that environment's four data buckets plus the
training/endpoint/ci-deploy roles' encrypt/decrypt access.

**Over:** (a) one key per bucket (raw/processed/artifacts/data-capture each
with their own key), and (b) a single key shared across dev and staging.

**Because:** all four buckets in one environment share the same trust
boundary — the same account, the same small set of consuming roles
(training, endpoint) — so per-bucket keys would mean four nearly-identical
key policies to maintain for zero real isolation gain (any role trusted for
one bucket's key is trusted for all of them anyway, since the roles that
read raw/processed also write artifacts/data-capture). A single
cross-environment key was rejected in the other direction: it would blur the
blast radius between dev and staging (a staging key-policy mistake could
affect dev-encrypted objects) and would survive `make destroy ENV=staging`
even though nothing in staging still needs it — I'd rather the key expire
with its environment.

**I'd flip this if:** a bucket needed a genuinely different set of principals
than the others (e.g. if `data-capture` were ever read by a third-party
auditor role that shouldn't be able to decrypt `raw`) — at that point
per-bucket keys buy real isolation instead of just duplication, and the
extra key-policy maintenance would be worth it.

## Bucket lifecycle windows

**Chose (per bucket):**
- `raw`: expire current versions after 30 days, no transition.
- `processed`: no expiration; transition to STANDARD_IA after 60 days.
- `artifacts`: no expiration; transition to STANDARD_IA after 90 days, then
  GLACIER after 365 days.
- `data-capture`: expire after 60 days.
- All buckets: noncurrent versions purged after 30 days; incomplete
  multipart uploads aborted after 7 days.

**Over:** a single uniform retention window across all four buckets (e.g.
"expire everything after 30 days" or "keep everything forever").

**Because:** the four buckets play structurally different roles in the
pipeline, so a uniform window is either too aggressive for some or too
wasteful for others. `raw` is disposable input — once a document has been
OCR'd and classified, the raw upload has no further value beyond
short-window reprocessing/debugging, so it expires fast. `processed` and
`artifacts` are lineage: processed output can become training data, and
artifacts (`model.tar.gz`, eval reports) are what M1's registry lineage
points back to — deleting either breaks "can I explain how this model
version came to exist," so neither expires, they only get cheaper to store
over time via storage-class transitions. `data-capture` is a monitoring
input with a natural shelf life: M5's drift detection needs enough history
to compare against the M1 baseline (a few weeks), but capture volume is
100%-of-traffic in dev, so keeping it indefinitely is the fastest way to
blow the $15 cost guardrail for no analytical benefit past the comparison
window.

**I'd flip this if:** a real customer's document-retention/compliance
requirement forced a different number (many are 7-year record-retention
regimes for `medical_report` in particular) — those numbers would come from
that requirement, not from a cost-minimization default, and would likely
mean per-document-class lifecycle rules inside `processed`/`artifacts`
rather than one rule per bucket.

## Bucket naming: account-id suffix, as an exception to `intake-<component>-<env>`

**Chose:** S3 bucket names are `intake-<component>-<env>-<account_id>` (e.g.
`intake-raw-dev-123456789012`); the `Name` tag on each bucket carries the
canonical `intake-<component>-<env>` form.

**Over:** the literal convention name with no suffix.

**Because:** S3 bucket names must be globally unique across every AWS
account on the planet, not just within this account — every other resource
in this stack (IAM roles, the KMS alias, the ECR repo) is scoped to this
account, so the plain convention name is guaranteed collision-free there,
but for S3 specifically it is not. Appending the account id (available
without a secret, via `data.aws_caller_identity`) guarantees `terraform
apply` succeeds on any clean account without a human picking a unique
suffix by hand, which is the M0 deliverable's "no manual step" bar.

**I'd flip this if:** this project needed cosmetically clean bucket names for
some customer-facing reason (e.g. a public data-sharing URL) — not the case
here, all four buckets are fully private (`public_access_block` all true).

## CI deploy role: scoped to what M0 creates, not to future `terraform apply`

**Known gap, not a final decision.** The `ci-deploy` role created in this
milestone can push/pull the ECR image and read/write only its own
environment's Terraform state key — it cannot yet run `terraform apply`
against the full stack, because most of the stack's future resources
(SageMaker, Step Functions, Lambda, EventBridge, DynamoDB) don't exist yet
and their ARNs can't be scoped ahead of time without guessing or falling
back to `Resource: "*"`. M6 (CI/CD) will extend this role's inline policy
incrementally, one action/resource pair per resource type it needs to
manage, scoped to `intake-*-<env>` ARN patterns — never a blanket
`AdministratorAccess`-shaped policy for CI, even though that would be the
expedient way to unblock `terraform apply` from a pipeline.

## Lambda roles: deferred to the milestone that creates each Lambda

M0 does not create any Lambda function or Lambda IAM role, even though the
milestone's task list mentions "each Lambda" as an in-scope role category.
No Lambda exists in the repo yet (M3 introduces the review-API and
routing/glue functions where SDK integrations aren't sufficient). A role
created now, before its function and the resources it touches exist, would
either be empty (no value) or have to guess at resource ARNs. Each Lambda's
IAM role is created alongside that Lambda, in the milestone that defines it,
following the same `iam_role` module pattern established here.
