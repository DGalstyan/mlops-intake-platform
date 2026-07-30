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

## CI deploy role: grow its permissions per-milestone, don't front-load them

**Chose:** a `ci-deploy` role that at M0 can do exactly two things — push/pull
this environment's ECR image, and read/write only this environment's Terraform
state key. Its policy grows one action/resource pair at a time, in the
milestone that introduces the resource it needs to manage, always scoped to
`intake-*-<env>` ARN patterns.

**Over:** (a) granting the role the permissions it will eventually need for a
full `terraform apply` now, which with no resources yet in existence means
falling back to `Resource: "*"` or service-level wildcards; (b) attaching
`AdministratorAccess`/`PowerUserAccess` "until CI works", the expedient option.

**Because:** the role is live on a **public** repo from the moment it exists. A
wildcard granted "temporarily" to unblock a pipeline is exactly the thing the
rubric names as an instant point-loser, and there is no forcing function that
ever makes anyone come back and narrow it. Growing the policy alongside the
resources means every grant can name a real ARN.

**I'd flip this if:** the CI pipeline needed to create genuinely unpredictable
resource names (e.g. Terraform-generated random suffixes), where no ARN pattern
can be written ahead of time. Then I would scope by resource *tag* condition
(`aws:RequestTag/project = intake`) instead of by ARN, which is the same
principle with a different key.

**Consequence, stated honestly:** as of M0 this role cannot run `terraform
apply` for the stack's resources at all. M6 closes that. Until then the deploy
path is a human running `make apply` locally.

## Lambda roles: defer each role to the milestone that creates its function

**Chose:** create no Lambda IAM roles at M0, even though the milestone task
list names "each Lambda" as an in-scope role category. Each Lambda's role is
created alongside that Lambda, in the milestone that defines it, using the same
`iam_role` module.

**Over:** creating five empty role shells now so the M0 checkbox could be
ticked.

**Because:** a role whose policy is scoped to a function that does not exist
can only be empty or wrong — it either grants nothing, or guesses a resource
ARN that later turns out different, and a wrong guess is worse than an absence
because it looks deliberate. There is also no way to write the trust
relationship's `aws:SourceArn` condition without the function ARN.

**I'd flip this if:** the Lambda names and their target resources were fixed by
an external contract up front (a fixed event schema from another team, say), in
which case the ARNs are knowable and creating the roles early would let the
security review happen before the code exists.

## `Resource: "*"` inventory: four unavoidable categories, and why each is

**Chose:** leave every `Resource: "*"` in place, each with an inline comment, and
inventory them *by category* here rather than contort the code to satisfy a grep.

**Over:** (a) splitting `aws_kms_key` into `aws_kms_key` + a separate
`aws_kms_key_policy` so the policy could reference `aws_kms_key.this.arn`;
(b) leaving them undocumented; (c) keeping a running *count* in the code comments,
which an earlier revision did and which went stale the moment M3 added more sites.

**Because:** the rubric names `Resource: "*"` as an instant point-loser, so an
undocumented one reads as laziness. But all of them are genuinely mandated by AWS,
and they reduce to four categories:

| Category | Where | Why `*` is unavoidable |
|---|---|---|
| KMS key-policy statements | `modules/kms/main.tf` (3) | These are **key policy** statements — a resource-based policy attached to exactly one key. `"*"` is AWS's documented spelling of "this key", and a key's ARN cannot be referenced inside its own policy without a cycle. |
| `ecr:GetAuthorizationToken` | `modules/stack/iam.tf` (3) | The action has no resource type in the ECR IAM reference. It is an account-level pre-auth call; no condition key narrows it either. |
| CloudWatch Logs **delivery** API | `modules/intake/statemachine.tf` (1) | `logs:CreateLogDelivery` and friends reject resource-level permissions. Step Functions needs them to attach its vended log group. Note this is the statement **deleted at M0** as premature and re-added here by the milestone that created a state machine to use it. |
| X-Ray segment submission | `modules/intake/statemachine.tf` (1), `modules/intake/lambda.tf` (3) | `xray:PutTraceSegments` / `PutTelemetryRecords` have no resource type. Behind `enable_xray`, so they disappear entirely if tracing is off. |
| `textract:DetectDocumentText` | `modules/intake/statemachine.tf` (1) | No resource type. The control that matters is the separate, scoped `s3:GetObject` on the raw bucket — Textract can only read what the role can read. |

There is additionally one `Principal: "*"` / `Action: "s3:*"` in
`infra/bootstrap/main.tf`. That is a **Deny** rejecting non-TLS requests to the state
bucket — the AWS-recommended `aws:SecureTransport` pattern. A deny-everyone is the
opposite of a permission grant, and narrowing its principal would weaken it.

Option (a) is mechanically possible for the KMS case and would remove three sites. I
rejected it because AWS documents `"*"` as *the* key-policy form, and restructuring a
working key policy to satisfy a text search trades real correctness risk for a
cosmetic win.

**Run `make wildcard-audit`** to regenerate the file:line list. Deliberately a
command rather than a number written down, because a hardcoded count is a claim that
rots — which is exactly what happened to the previous version of this entry.

**I'd flip this if:** an organisation-level SCP or a compliance scanner hard-failed on
the literal string regardless of policy type. Then option (a) for KMS, and the
X-Ray/ECR/Textract statements moved into separate minimal-scope roles.

## Deleted over-engineering: the state-machine role's logging policy

**Deleted:** an earlier revision of `modules/stack/iam.tf` gave the
state-machine role the CloudWatch Logs *delivery* actions
(`logs:CreateLogDelivery`, `logs:PutResourcePolicy`, and friends), which AWS
requires on `Resource: "*"`.

**Because:** it granted the ability to attach a resource policy to **any log
group in the account** to a role that had nothing to run — no state machine
exists until M3. The comment above it even described the role as "trust-only",
which the code contradicted. That is a wildcard with no corresponding
capability: pure downside, and a reviewer grepping for `"*"` would have found
it attached to an idle role.

The role is now genuinely trust-only (`inline_policy_json = null`). M3 adds the
logging block alongside the state machine that needs it, at which point the
wildcard buys something. This dropped the repo's wildcard count from seven to
six.

**The general rule this came from:** a permission whose target does not exist
yet should not exist yet either.

## `force_destroy` everywhere, and no `prevent_destroy` anywhere

**Chose:** `force_destroy = true` on all four data buckets and the state
bucket, `force_delete = true` on the ECR repository, and no `prevent_destroy`
lifecycle block on anything.

**Over:** the production-shaped default — `prevent_destroy` on stateful
resources, `force_destroy = false` so a non-empty bucket blocks its own
deletion.

**Because:** this is a take-home graded explicitly on "`destroy` actually
leaves nothing behind", with a ~$15 budget. A versioned bucket holding objects
refuses to delete without `force_destroy`, and an ECR repo holding images
refuses without `force_delete` — so the safe-looking defaults are precisely
what would leave a dirty account and a failed teardown. The data here is
synthetic and regenerable by `src/data/generate.py`, so there is nothing to
protect.

**I'd flip this if:** any of these buckets held real customer documents — then
`prevent_destroy` on `processed`/`artifacts`, `force_destroy = false`
everywhere, and deletion gated behind a separate pipeline with a human
approval, because the failure mode inverts: an accidental `terraform destroy`
becomes far more expensive than a dirty account.

## The KMS key survives `make destroy` for 7 days

**Chose:** `deletion_window_in_days = 7`, AWS's minimum, and to document
loudly that the key therefore outlives `make destroy`.

**Over:** a longer window (the default is 30), or pretending teardown is
instantaneous.

**Because:** zero is not permitted — AWS enforces a 7-to-30-day
`PendingDeletion` state so that a key destroyed by mistake can be recovered
before the data it encrypted becomes permanently unreadable. 7 days is the
floor, so it is the fastest honest answer to "is the account clean?". The key
keeps billing (~$0.23 prorated per environment) during that window, and its
alias is deleted immediately, so it appears in the console without a friendly
name. This is stated in the `make destroy` output, the Makefile comment, and
the README teardown section rather than left for a reviewer to discover.

**I'd flip this if:** the key encrypted anything irreplaceable, where 30 days
of recovery room is worth the extra standing cost.

## The GitHub OIDC provider is an account singleton, so its creation is optional

**Chose:** create the provider in the bootstrap root behind a
`create_github_oidc_provider` toggle (default `true`), and have the environment
roots resolve it with a `data` source rather than reading the bootstrap output
or constructing the ARN as a string.

**Over:** (a) unconditionally creating it; (b) constructing
`arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com` by
string interpolation, which is deterministic and needs no lookup.

**Because:** there is exactly one OIDC provider per account per issuer URL. Any
reviewer whose sandbox already federates GitHub Actions gets
`EntityAlreadyExists` on `make bootstrap`, which breaks the "no manual step
beyond credentials" deliverable on their very first command. The toggle makes
that a one-line fix instead of a debugging session.

The string-constructed ARN was rejected for a subtler reason: IAM validates
that a federated principal exists when the role is created, so if bootstrap has
not run, role creation fails with `MalformedPolicyDocument: Invalid principal in
policy` — an error naming neither the OIDC provider nor the bootstrap step. The
`data` source fails first and says exactly what is missing. It also makes the
stack indifferent to whether bootstrap or the account created the provider.

**I'd flip this if:** the provider were managed by a separate platform team's
Terraform, in which case the environment roots should read it from that team's
remote state output and fail if it is absent, rather than tolerating either
origin.

## The state bucket name is derived, never read from state

**Chose:** compute the state bucket name as `<project>-tfstate-<account_id>` in
three places — the bootstrap root, the stack module, and the Makefile — from
the same two inputs.

**Over:** reading it from the bootstrap root's `terraform output`, which is what
an earlier revision of the Makefile did.

**Because:** the bootstrap root keeps **local** state, and `*.tfstate` is
gitignored. So `terraform output -raw state_bucket_name` only works on the one
machine that ran `make bootstrap`: it returns nothing on a fresh clone and on
every CI runner, which silently makes `make plan`/`apply`/`destroy` unusable in
the pipeline M6 has to build. It also means losing that laptop loses the ability
to address your own state. Deriving the name needs nothing but credentials.

The cost is a naming contract duplicated in three files, which is why each of
the three carries a comment pointing at the other two. Duplication that a
`grep` finds is a better failure mode than a lookup that only works in one
place.

**I'd flip this if:** the bootstrap root used remote state itself (a
chicken-and-egg problem, but solvable by hosting it in a separate
platform-level account), at which point a `terraform_remote_state` data source
gives one source of truth with no duplication.

## `component` is tagged per-resource, not via `default_tags`

**Chose:** set `project`, `environment` and `managed_by` in each root's
provider `default_tags`, and pass `component` through each module's own `tags`
argument instead.

**Over:** putting all four in `default_tags`, as the M0 task list's wording
suggests.

**Because:** `default_tags` applies one fixed value to every resource the
provider creates. `component` is by definition different per resource — the KMS
key is `component = kms`, the raw bucket is `component = raw`, the training
role is `component = training` — so it cannot come from a provider-level
default without being wrong on all but one resource. Every taggable resource
does carry it; the untaggable sub-resources
(`aws_s3_bucket_versioning`, `aws_kms_alias`, `aws_s3_bucket_public_access_block`)
accept no tags at all, from either mechanism.

**I'd flip this if:** the tag were genuinely uniform per root — which is the
case in `infra/bootstrap`, and that root does put `component = state-backend`
in `default_tags`.

---

# Decision log — M1 (Training, evaluation, registry)

## The baseline artifact stores distributions, and deliberately not accuracy

**Chose:** `baseline_statistics.json` carries prediction priors, document
char-length and token-count distributions (quantiles **plus fixed histogram
edges**), a confidence histogram, per-feature TF-IDF means and variances for the
top 200 features, and training vocabulary size. It carries no F1 and no accuracy.

**Over:** (a) storing the evaluation metrics in the same file, which is the
obvious convenience; (b) storing only summary statistics without histogram edges;
(c) storing all 20,000 feature moments.

**Because:** drift is a change in a *distribution*, so a baseline holding scalars
supports no drift test at all. Each element earns its place against a specific
question M5 has to answer:

- **prediction priors** → prediction drift, and the only signal available when
  there is no ground truth, which is the normal production case.
- **length/token distributions** → input drift, computable with no model at all,
  so it still works when the endpoint is down.
- **confidence histogram** → the concept-drift proxy. Confidence decaying while
  inputs and predictions look stable means the world changed in a way the
  features do not capture.
- **per-feature moments** → lets drift be *attributed* to specific vocabulary
  rather than only reported as "something moved".
- **vocabulary size + coverage** → a TF-IDF model silently ignores unseen tokens,
  so falling coverage means the model is going blind to its input while its
  confidence stays high. Nothing else in the artifact reveals that.

Keeping accuracy out is the important part. Mixing model-quality metrics into the
drift reference invites exactly the mistake M5 must avoid — treating "the data
changed" and "the model got worse" as one signal. They live in `metrics.json`.

Histogram **edges are stored with the artifact** because recomputing bins from
live data would compare two differently-binned distributions and manufacture
drift from nothing.

**I'd flip this if:** we moved to embedding-based drift, where the reference
becomes centroids plus covariance rather than per-feature moments, and the
per-feature table stops being meaningful.

## Two registry versions differ by calibration, not by a different seed

**Chose:** produce the second version by disabling probability calibration
(`--no-calibration`).

**Over:** changing the random seed, or the feature cap, to get two runs with
slightly different numbers.

**Because:** the deliverable asks for two *distinguishable* versions, and a
difference that only shows up in the fourth decimal place of macro-F1 is
indistinguishable from noise — it would not demonstrate that the registry
captures anything meaningful. Calibration moves **ECE**, which is the metric the
Route state's confidence threshold actually depends on, so the two versions
differ in a way a reviewer can reason about: one is safe to gate auto-approval
on, the other is not, at similar accuracy.

**I'd flip this if:** the point were to demonstrate the retrain gate's *margin*
logic rather than the registry, where a controlled macro-F1 delta is the more
direct fixture.

## Calibration is part of the model, not a post-hoc nicety

**Chose:** wrap the linear classifier in `CalibratedClassifierCV` (isotonic,
cv=3) inside the model implementation, and report ECE alongside F1.

**Over:** shipping raw `LogisticRegression` probabilities and reporting accuracy
only.

**Because:** the intake Route state gates auto-approval on `max(predict_proba)`.
Raw probabilities from a high-dimensional sparse TF-IDF fit are systematically
overconfident, so an uncalibrated model auto-approves documents it should have
escalated — and that failure surfaces as a *routing* bug, or as a rising human
override rate, long before anyone suspects the model's probability scale. If a
confidence threshold is load-bearing, its calibration is a correctness property,
not a tuning detail.

**I'd flip this if:** routing stopped depending on a probability (e.g. a
learned-to-defer model that emits an explicit abstain class), at which point ECE
stops being the metric that matters.

## `predict_proba` is in the model interface, not optional

**Chose:** the `DocumentClassifier` Protocol requires
`fit / predict / predict_proba / save / load`.

**Over:** a narrower `fit / predict` interface with probabilities as an optional
capability discovered at runtime.

**Because:** a model that cannot produce a probability cannot be dropped into
this pipeline at all — the Route state has nothing to gate on. Making it optional
would move that failure from "the swap does not type-check" to "the swap
deployed and every document auto-approved". The interface should refuse the
substitution up front.

**I'd flip this if:** routing moved to a separate calibrator/deferral model, so
the classifier genuinely only needed to emit a label.

## Training-set and golden-set metrics are both kept, and both labelled

**Chose:** `train.py` writes `metrics.json` with `"split": "train"` and
`"is_held_out": false`; `evaluate.py` writes its own with `"split": "golden"` and
`"is_held_out": true`. `register.py` **refuses** to attach anything that is not
the golden-set variant.

**Over:** (a) only emitting held-out metrics; (b) emitting both without
distinguishing them.

**Because:** training-set numbers are genuinely useful for "did this fit
converge", so throwing them away loses debugging signal. But an unlabelled
training-set macro-F1 sitting in a file called `metrics.json` is the easiest way
to accidentally publish a fictional score — and the retrain gate reads exactly
that field to decide whether a candidate beats the champion, so the consequence
is a gate that silently stops meaning anything. The refusal in `register.py` is
what makes the labelling load-bearing rather than advisory.

## The snapshot id is a content hash, not a UUID or a timestamp

**Chose:** `snapshot_id = sha256(canonical documents + generation parameters)`,
recorded in `snapshot.json` and carried into the registry as
`CustomerMetadataProperties.data_snapshot_id`.

**Over:** a UUID or an ISO timestamp assigned at generation time.

**Because:** the id has to *prove* two runs used the same input, and a UUID only
records that someone generated data twice. With a content hash, an identical id
means identical bytes and a changed document changes the id — which is what makes
it a usable lineage key when someone asks "was this model trained on the data we
think it was?". The generation parameters are hashed in too, so two corpora that
coincidentally contain the same documents under different splits do not collide.

**I'd flip this if:** the dataset grew large enough that hashing every document
on each run became slow, at which point the hash would move to a manifest of
per-file digests.

## Seeds are derived with SHA-256, never with `hash()`

**Chose:** derive every sub-seed via `sha256("|".join(repr(part)))`.

**Over:** the shorter `random.Random(hash((seed, "split", i)))`.

**Because:** Python randomises `str` and `bytes` hashing per process unless
`PYTHONHASHSEED` is fixed. A tuple hash containing a string therefore produces
*different* data in every new interpreter — while looking perfectly
deterministic within a single test session. That would have silently broken the
reproducibility the entire snapshot-id mechanism rests on, and the output would
still have been valid-looking data, so nothing downstream would flag it. There is
a test (`test_determinism_survives_a_fresh_interpreter`) that runs two
subprocesses with `PYTHONHASHSEED=random` specifically to catch a regression
here.

**Found the hard way:** the first version of the generator used `hash()`.

## Dependencies are pinned in-repo and installed into the training container

**Chose:** pin exact versions in `requirements.txt`, ship it in the training
job's `source_dir` so SageMaker pip-installs it, and record the *resolved*
versions into `lineage.json` at run time.

**Over:** relying on the managed SageMaker scikit-learn container's preinstalled
versions.

**Because:** "reproducible" has to mean the same versions resolve on a laptop and
in the job, and a managed container's contents change between framework
releases. Pinning states the intent; recording what actually resolved is what a
reproduction attempt needs, because those two can differ and only the second is
evidence.

**I'd flip this if:** install time on cold container starts became the bottleneck,
at which point the pins move into a custom image built in CI and referenced by
digest — which is what M2 does for inference anyway.

---

# Decision log — M2 (Deployment)

## Real-time endpoint, not Serverless Inference

**Chose:** a real-time endpoint on `ml.t3.medium` with `deploy_endpoint` defaulting
to false so it is never created by accident.

**Over:** SageMaker Serverless Inference, which the assignment explicitly
encourages on cost grounds and which would genuinely be cheaper for a mostly-idle
graded run.

**Because:** serverless cannot satisfy three separate M2 requirements at once, and
each loss lands on a different graded milestone:

1. **Data capture is not supported on serverless endpoints.** M5's drift detection
   reads data-capture files as its only source of production traffic. Choosing
   serverless deletes the input to a 20%-weighted milestone.
2. **Application Auto Scaling does not apply.** Serverless scales on its own
   concurrency model, so "autoscaling on a justified metric, with a documented
   reason for the target value" has nothing to configure and nothing to justify.
3. **Blue/green deployment guardrails are real-time only.** Canary traffic shifting
   with alarm-driven automatic rollback is M2's headline deliverable and simply
   does not exist for serverless endpoints.

Cold starts, the usual argument against serverless, are not the reason. They would
be manageable — the model loads in well under a second. The disqualifier is that
serverless silently removes the observability and release-safety surface that 40%
of the grade sits on.

**Cost is mitigated, not ignored:** the smallest viable instance
(~$0.05/hour), `max_capacity = 2` as a hard cost ceiling, `deploy_endpoint = false`
by default, and `make destroy`. The standing hourly charge is in the README cost
table.

**I'd flip this if:** the workload were genuinely spiky with long idle periods *and*
drift monitoring could be fed from the pipeline's own results store rather than
endpoint data capture. That second condition is the real dependency, and it is a
design change, not a config change.

## The autoscaling target is measured, and the first guess was 4x wrong

**Chose:** target-track `SageMakerVariantInvocationsPerInstance` at **150
invocations/minute per instance**.

**Over:** (a) a CPU-utilisation target, the common default; (b) the value I first
wrote, 900, which was a guess.

**Because:** CPU utilisation on a sparse dot product barely moves under load — the
endpoint queues before it saturates a core — so a CPU-based policy scales far too
late or never. Invocations-per-instance is the signal that actually correlates with
queueing for this model.

The number itself comes from `scripts/measure_throughput.py`: ~650
invocations/minute measured against the real handler path, derated by 0.35 for an
`ml.t3.medium`'s two burstable vCPUs (~227/min of real capacity), times 0.60
headroom. The headroom exists because target tracking is a *steady-state* signal
and bringing an instance into service takes minutes; a target set at capacity
guarantees the endpoint is already queueing before help arrives.

**The measurement corrected me.** My initial default of 900 is roughly **4x** real
per-instance capacity — the policy would effectively never have scaled out, and
"autoscaling on a justified metric" would have been decorative. Same story for the
latency alarm: I guessed 2000 ms before measuring a p99 of 220 ms, and settled on
1500 ms as 7x measured p99. Numbers and caveats in `evidence/m2/throughput.json`
and `evidence/m2/throughput.md`.

**I'd flip this if:** a real concurrent load test against a deployed endpoint
disagreed — which it may well, since the measurement is sequential, in-process, and
on faster hardware than a t3.medium. It is an upper bound on capacity, and the
derating factor is the guess that remains.

## Malformed input returns 4xx, and that is a release-safety decision

**Chose:** the serving layer maps client errors (bad JSON, wrong content type,
oversized payload, empty document) to **4xx**, and only genuine internal failures to
**5xx**.

**Over:** the simpler "any exception is a 500".

**Because:** the endpoint's `ModelInvocation5XXErrors` alarm drives the automatic
rollback. If a malformed request produced a 5xx, anyone posting bad JSON at a
deployment could roll back a perfectly healthy version — and worse, the rollback
would look justified in the alarm history. The status-code mapping is therefore not
an HTTP-hygiene preference; it decides whether the rollback guardrail is
trustworthy. There is a test asserting it, and the container smoke script checks it
again against the running image.

**I'd flip this if:** never, for this design. If the alarm moved to a metric that
excluded client errors, the mapping would still be right for every other reason.

## Readiness means "can predict", not "process started"

**Chose:** `/ping` returns 200 only after the model has loaded **and** successfully
scored a canary document. Failures are captured and reported through `/ping` rather
than crashing the container.

**Over:** returning 200 as soon as the process is up, or as soon as the artifact
deserialises.

**Because:** during a canary deployment, a container that reports ready before it
can actually serve receives traffic, fails every request — and that failure is
attributed to the *new* variant looking healthy long enough to proceed. An artifact
can deserialise and still fail every call (version-mismatched pickle, empty
vectoriser vocabulary), so a load-only check is the dangerous variant. Readiness
that does not exercise the model is worse than no readiness check, because it is
trusted.

Capturing the failure instead of exiting is deliberate too: a container that exits
at startup gives SageMaker nothing to query and produces a generic "container
failed", whereas one that starts and reports *why* it is unhealthy is diagnosable
from CloudWatch without reproducing it.

**I'd flip this if:** the readiness probe's own cost became significant — a model
with a multi-second first inference would make every instance replacement slower,
and the probe would need to move to a cheaper invariant.

## Approved-only deployment is enforced in a script, not a Terraform data source

**Chose:** `scripts/resolve_approved_model.py` resolves the latest **Approved**
version and exits non-zero, naming the statuses it found, if there is none. Its
output is passed to Terraform as an explicit `model_package_arn`.

**Over:** a `data "aws_sagemaker_model_package"` lookup inside Terraform that
re-resolved "latest approved" on every plan.

**Because:** with an in-Terraform lookup, the deployed version becomes an
invisible moving input — someone approves a version in the console and the next
unrelated `terraform apply` silently ships it. Passing the ARN explicitly means a
version change appears as a diff in the plan, which is what makes a release a
decision rather than a side effect. The refusal is loud for the same reason: a
resolver that returned nothing would let the deploy proceed with an empty variable
and fail confusingly later.

**I'd flip this if:** the deploy were driven by the EventBridge approval event
(which M5 wires up), where the approved version ARN arrives *in* the event payload.
That is still explicit — the value comes from the approval itself rather than from
a re-query.

## Custom container over a managed framework image

**Chose:** build our own image (`src/inference/Dockerfile`) implementing `/ping` and
`/invocations` with Flask + gunicorn.

**Over:** a managed SageMaker scikit-learn container plus an `inference.py`, which
is less code and explicitly permitted by the assignment.

**Because:** the managed images pin their own scikit-learn version, and a model
artifact must be unpickled by the version that wrote it. Using a managed image
would make the version pinning in `requirements.txt` a fiction — training and
serving could silently diverge, and the failure mode is a subtly wrong
deserialisation rather than a clean error. Owning the image makes training and
serving scikit-learn provably identical, and it gives M6 a container to build and
push by digest.

The cost is more code to own: a serving layer, a Dockerfile, and a local contract
script. That is why the handlers are kept free of HTTP concerns — they stay
testable without the container, which is what let the contract be verified while
the image build itself was blocked.

**I'd flip this if:** the model gained heavyweight framework dependencies (torch,
transformers) where the managed image's tested CUDA/driver combination is worth far
more than version symmetry.

**Not verified:** the image has never been built. `docker build` hung on this
machine and the daemon became unresponsive; the build was abandoned rather than
retried indefinitely. See the README's known gaps.

---

# Decision log — M3 (Orchestration & human-in-the-loop)

## Two Lambdas in the workflow, and both earn their place

**Chose:** direct SDK integrations for Textract, SageMaker Runtime, Bedrock, DynamoDB
and SQS. Exactly two Lambdas inside the state machine, plus one outside it:

1. `NormalizeOcr` — Textract returns a block *graph*. Assembling reading-order text
   requires sorting blocks by geometry with row banding and joining them; ASL cannot
   sort an array of objects by a nested numeric field. It also computes the content
   hash and the char/line counts M4 turns into metrics.
2. `ValidateExtraction` — JSON Schema validation and cross-field rules. A Choice
   state can compare two values; it cannot evaluate a regex, an enum, or
   "expiry_date must be after date_of_birth".
3. The review API, which is outside the state machine because it is an HTTP endpoint.

**Over:** the conventional shape, where a Lambda sits in front of each service call
to marshal its request and response.

**Because:** glue-only Lambdas are named as a point-loser, and rightly — each one is
a cold start, a log group, an IAM role, a deployment artifact and a place for the
retry policy to be subtly different. Two things fell out of removing them that I did
not expect:

- **Routing needed no Lambda at all.** Confidence and business-rule routing are two
  Choice states. The endpoint returns its own `auto_approve_eligible` boolean,
  computed against the config threshold, so the ASL compares a boolean rather than
  duplicating the number — and there is a test asserting the ASL contains no
  hardcoded threshold.
- **The extraction prompt needed no Lambda either.** Prompts are rendered from
  `schemas/*.json` at deploy time into DynamoDB and read with a direct GetItem. So
  adding a field, or a whole document class, touches one JSON file and nothing else.

**I'd flip this if:** a step needed genuine branching logic over a payload — at which
point a Lambda is honest and a 12-state ASL detour is not.

## Idempotency is claimed before anything billable, and guards three writes

**Chose:** a conditional `PutItem` on a ledger table keyed by
`bucket#key#versionId`, as the **first** state in the workflow. Plus conditional
writes on the result and on the review task. Plus a deterministic execution name
derived from the same key.

**Over:** (a) checking for an existing result at the end; (b) relying on the
execution-name dedupe alone.

**Because:** each layer catches what the others miss. The execution name stops most
duplicates before an execution starts, but only within its dedupe window. The ledger
claim catches the rest — and because it runs first, a duplicate costs one DynamoDB
write instead of a Textract call plus an endpoint invocation plus a Bedrock call.
The conditional write on the *review task* is the one that is easy to forget and is
called out explicitly in the assignment: two review tasks for one document wastes a
human's time and produces two conflicting corrections.

A duplicate ends in a `Succeed`, not a `Fail`. Duplicate S3 deliveries are routine,
and failing them would put a permanent error rate on the state machine's metrics and
make a real failure impossible to see.

**No TTL on the ledger, deliberately.** Expiring an idempotency record re-opens the
duplicate window for any redelivery after the TTL. The entries are ~200 bytes each;
trading correctness for that storage would be a bad deal.

**I'd flip this if:** documents were legitimately reprocessable — a "reprocess this
document with the new model" flow would need the key to include a processing
generation, not just the object version.

## The dead-letter path only reads fields that are seeded up front

**Chose:** a `Prepare` state that seeds empty `ocr` and `classification` objects
before anything can fail.

**Over:** letting the dead-letter state read those paths directly, which is what the
first revision did.

**Because:** JSONPath references to absent fields fail the state. So a failure during
OCR would have failed the dead-letter write *too* — losing the document at exactly
the moment the dead-letter path is the only thing that could save it. This was a real
bug in the first version of the ASL, caught by writing a test that walks every
`MessageBody` reference and checks it against the seeded set rather than by reading
the definition.

**I'd flip this if:** the workflow moved to JSONata, where a missing-path expression
evaluates to nothing rather than erroring.

## Extraction failure sends the document to review; it does not dead-letter it

**Chose:** Bedrock failure after all retries routes to `ExtractionUnavailable` and
then into the human-review queue with an empty field set and the reason.

**Over:** dead-lettering the document, which is what every other terminal failure
does.

**Because:** classification already succeeded, so there is a usable partial result
and a human can supply the fields by reading the document. Discarding it would throw
away work that was already paid for, and it is the difference between "Bedrock was
throttled for ten minutes" and "we lost the document". The same reasoning makes an
unparseable model response a *validation failure* rather than a Lambda error.

**I'd flip this if:** review capacity were the binding constraint, where flooding the
queue during a Bedrock outage would be worse than deferring the documents — at which
point the right answer is a retry queue, not a dead letter.

## The review API never accepts a task token from the caller

**Chose:** the API takes `correlation_id` and looks the task token up from the review
table.

**Over:** accepting the token in the request body, which is simpler and is what the
task-token examples usually show.

**Because:** a task token is a capability. Whoever holds one can resume that execution
with arbitrary output — including a corrected class that becomes a training label.
Accepting a caller-supplied token would let anyone who obtained or guessed one inject
a correction into any document in flight. There is a test asserting a
caller-supplied token is ignored.

Related: `prediction_was_correct` is computed by comparing the correction to the
stored prediction, never submitted. M5 uses the override rate as its concept-drift
proxy, and a self-reported number would make that signal meaningless.

**I'd flip this if:** the reviewer UI were a trusted server-side component holding
its own credentials — but it would still be the wrong shape, because looking the
token up costs one GetItem and removes the whole class of problem.

## The correction is persisted before the result

**Chose:** `PersistCorrection` runs before `StoreReviewedResult`, and the review is
marked complete only after `SendTaskSuccess` returns.

**Over:** the more natural order of storing the outcome first.

**Because:** the reviewer's labour is the harder thing to recreate. A document can be
redelivered; a human's judgement cannot be recovered if it is dropped. And marking
the review complete before resuming the execution would leave a review closed while
the execution still waited — it would eventually time out and dead-letter a document
a human had already fixed. Both orderings have tests.

## A hand-rolled JSON Schema validator, with a hard failure on unknown keywords

**Chose:** implement the subset of Draft 2020-12 these four schemas use, and raise
`NotImplementedError` on any keyword outside that subset.

**Over:** (a) adding the `jsonschema` package to the Lambda bundle; (b) implementing
the subset and ignoring unknown keywords, which is the usual shortcut.

**Because:** the subset is small and fully enumerated, and the schemas are ours. But
option (b) is genuinely dangerous: a validator that silently skips a keyword it does
not understand reports a document as valid on fields it never checked, and that
document is then auto-approved. Failing loudly means the failure surfaces in CI when
someone adds a `$ref`, not in production as an unexplained auto-approval.

**I'd flip this if:** the schemas needed `$ref`, `allOf`/`oneOf`, or conditional
subschemas — at which point this should be *replaced* by the real library, not
extended.

## The traces are from a simulation, and the tests are what make them credible

**Chose:** build `scripts/simulate_intake.py`, which runs the real handlers,
validator, routing conditions and classifier against stubbed AWS boundaries, and
produces the two required traces.

**Over:** (a) shipping M3 with no evidence at all until credentials exist;
(b) hand-writing plausible-looking trace files.

**Because:** (b) is fabrication. (a) leaves the routing logic, the validator, the
correction flow and the idempotency semantics entirely unexercised — none of which
need AWS to be wrong. The simulation catches real bugs: it is how the `note` KeyError
in `build_task_output` and the dead-letter seeding bug were found.

The risk of a simulation is that it diverges from what deploys, at which point the
traces are evidence of something that does not exist. That is why
`TestSimulatorMatchesAsl` asserts the simulator and the ASL agree on the always-review
class set, the three review-reason markers, and the *order* in which `DecideOutcome`
evaluates its conditions.

**Stated plainly:** this is not the deliverable. The deliverable is a Step Functions
execution history. This is the closest honest substitute, and the README says so.

---

# Decision log — M4 (Observability)

## Rates are metric math over raw counters, never pre-computed

**Chose:** the state machine emits raw counters (`DocumentsProcessed`,
`AutoApproved`, `HumanOverride`, `LLMInputTokens`, …) and every rate plus the cost
figure is derived with CloudWatch metric math in the dashboard and alarms.

**Over:** computing `AutoApprovalRate` and `EstimatedCostPerDocument` at emit time and
publishing them as values.

**Because:** two things break with pre-computed values.

1. A rate is frozen at the aggregation period it was computed for. With counters, the
   dashboard shows a 15-minute rate and an alarm evaluates an hourly one over the same
   datapoints. With a pre-averaged rate, one of those is wrong.
2. The assignment requires `EstimatedCostPerDocument` to be computed from *real token
   counts × documented prices*. Emitting a computed cost bakes today's price list into
   stored datapoints, so when prices change, last week's cost becomes unrecomputable —
   and worse, the historical series silently mixes two price regimes.

It also happened to solve an ASL limitation: ASL cannot convert a boolean to a number,
so `AutoApproved = 1 or 0` was not expressible. Emitting a constant `1` from the
outcome-specific branch is both simpler and the better design.

**I'd flip this if:** the metric volume made per-document counters expensive — at very
high throughput, pre-aggregating in a stream processor and emitting summaries is the
standard answer.

## `correlation_id` is never a metric dimension

**Chose:** dimension metrics by `Environment` and `DocumentClass` only.
`correlation_id` appears in logs, traces and stored records — never in a dimension.

**Over:** dimensioning by `correlation_id`, which would make per-document metrics
queryable in CloudWatch directly.

**Because:** CloudWatch bills per metric-name × dimension-value combination. One
dimension value per document means one custom metric per document — the classic way to
turn a $3 dashboard into a four-figure bill, and it degrades the console to
unusability long before the invoice arrives. There is a test asserting no metric
dimension contains "correlation".

**I'd flip this if:** never. Per-document lookup is what logs and traces are for.

## The `measures` classification is a deployed tag, not inferred from prose

**Chose:** every alarm carries a `measures` tag — "model quality (primary proxy)",
"system health", "cost", "data safety" — and the generated inventory reads that tag.

**Over:** inferring the classification from keywords in the alarm description, which
is what the first version did.

**Because:** it miscategorised two alarms immediately. `execution_failures` came out as
"data safety" because its description mentions the dead-letter queue. The
model-quality vs system-health split is precisely what the observability section is
graded on, so deriving it from fuzzy text matching is the wrong place to be clever. A
tag is deployed config: authoritative, machine-readable, and visible in the console
next to the alarm it describes.

**The general lesson, learned repeatedly in this repo:** generated documentation must
read from structured data, not from prose. Three hardcoded wildcard counts went stale
the same way.

## The alarm inventory is generated from source, not from `terraform output`

**Chose:** `scripts/render_alarm_inventory.py` parses `infra/**/*.tf` and renders
`evidence/m4/alarm-inventory.md`, with a test asserting the committed file matches a
fresh render.

**Over:** (a) hand-writing the inventory; (b) generating it from `terraform output`
after an apply.

**Because:** (a) drifts the moment someone adds an alarm — and an inventory that
silently omits a new alarm is worse than one that is obviously out of date. (b) only
exists after an apply, so it could not be produced at all without credentials, and it
would describe whatever was last applied rather than what is in the repo.

The test is the part that matters. Without it this is just a script nobody runs.

**Found while building it:** the extractor initially dropped interpolated fragments,
so `local.runbook_note` — which is what puts the runbook link into every description —
was invisible. The test then "passed" while checking something that was not what
deploys. Fixed by resolving the interpolation.

## Prices live in one JSON file read by both Terraform and Python

**Chose:** `config/prices.json`, read by the observability module via
`jsondecode(file(...))` for the dashboard's cost math, and by Python for cost
estimates. It records the retrieval date and the region.

**Over:** Terraform variables for the dashboard plus matching constants in
`src/config.py`.

**Because:** duplicated constants that must agree eventually disagree. This repo has
already lost that bet three times with hardcoded wildcard counts and twice with stale
README claims. A price that disagrees between the dashboard and the cost table is
worse than either being wrong alone, because the disagreement is invisible.

There is a test asserting the priced model id matches the Terraform default for
`bedrock_model_id` — swapping the model without updating prices would leave the cost
panel wrong-but-plausible, and nobody investigates a number that looks reasonable.

**I'd flip this if:** prices came from the AWS Price List API at plan time. That is the
correct answer for a real system and overkill here.

## Observability failures must never fail a document

**Chose:** every metric-emission state's catch-all continues to the same next state as
its success path, so a CloudWatch throttle cannot turn a stored document into a failed
one.

**Over:** letting a metric failure propagate, which is what happens by default.

**Because:** every emit state runs *after* the document's outcome is durably written.
A gap in a graph is recoverable; a document that failed because its metric could not be
published is not. There is a test asserting each emit state's catch-all target equals
its success target.

**I'd flip this if:** a metric were load-bearing for a control decision — the endpoint
rollback alarms are, which is exactly why those live beside the endpoint as a control
input rather than here as observability.

## The dashboard leads with business outcome, and has no CPU panel

**Chose:** four sections in order — business outcome, model health, pipeline health,
cost — each with a markdown header explaining in plain language what it answers.

**Over:** the conventional layout, which opens with infrastructure health.

**Because:** the brief is "a dashboard a non-engineer could read", and the rubric names
a CPU-only monitoring section as a point-loser. Taking that literally means the first
thing on the page answers "is the platform doing its job?" and CPU appears nowhere at
all. The model-health section explicitly labels its metrics as *proxies* and names what
each is blind to, on the dashboard itself rather than only in the README — the person
reading it at 3am is not reading the README.

---

# Decision log — M5 (Drift detection & retraining)

## PSI over binned data, not KS over a reconstructed sample

**Chose:** input drift is PSI against the baseline's stored histogram edges, plus a
directional median shift. `ks_statistic` exists and is tested, but the report does not
use it for the baseline comparison.

**Over:** the obvious answer, which is what I built first — reconstruct samples from
the stored histogram and run a two-sample KS test.

**Because:** KS compares empirical CDFs and therefore needs samples on both sides. The
baseline deliberately stores a histogram rather than raw documents, so reconstructing
samples to run KS measures the *reconstruction*. On an unshifted control window it
reported 0.30 and 0.38 ("breached") where PSI on the identical data reported 0.009 and
0.002 ("stable"). Spreading the reconstruction uniformly within bins instead of at
midpoints roughly halved the error and still left a false positive on token counts,
which are small integers no within-bin reconstruction recovers.

A drift detector that fires on unchanged data is worse than no detector: it gets
muted, and the real signal is muted with it.

The median shift was added because PSI's `(a-b)·ln(a/b)` term is symmetric — it says
*that* two distributions differ, never which way, and "documents got longer" and
"documents got shorter" have different causes.

**I'd flip this if:** the baseline stored raw samples or a quantile sketch (t-digest),
at which point KS is exactly right and needs no reconstruction. That is the correct
upgrade if input drift ever needs to detect within-bin shifts.

**Found by:** running the detector against a control window. That test now exists.

## The baseline's confidence reference is held-out, not training data

**Chose:** the confidence histogram in `baseline_statistics.json` is computed on the
**golden set**, while every other distribution in the same artifact comes from the
training split. The artifact records `confidence_source` so a reader can tell which.

**Over:** computing everything on the training split, which is what M1 originally did
and is the consistent-looking choice.

**Because:** a model is systematically more confident on documents it memorised. On
this corpus the gap is p10 **0.865 on train vs 0.731 held out** — so comparing
production against the training figure reports a 15% "decay" that is really
memorisation, and that is enough to breach the decay threshold on an unshifted window.
The drift job would have alarmed on day one, forever, on a perfectly healthy model.

The asymmetry is deliberate rather than an inconsistency: the input distributions
answer "what does normal input look like to this model", and the data it was fitted to
is the right answer to that. The confidence reference answers "how certain is this
model on data it has not seen", and training data cannot answer that at all.

**I'd flip this if:** never for confidence. The general principle — any baseline
statistic that depends on model *behaviour* must come from held-out data, while
statistics about the *inputs* may come from training data — is worth stating as a rule.

**Cost:** `BASELINE_SCHEMA_VERSION` moved to 1.1.0. Same shape, so a 1.0.x reader still
works, but its confidence comparison is against an inflated reference — which is why
`confidence_source` is recorded rather than the change being silent.

## Prediction drift alone never counts as decay

**Chose:** the classifier treats input drift and prediction drift together as "data
changed", and only the concept proxies (override rate, confidence decay) as "model
decayed". A shifted class mix on its own produces `DATA_CHANGED`.

**Over:** treating a shifted prediction distribution as evidence of model degradation,
which is how it is often used.

**Because:** if the input mix changed, the prediction mix *should* follow — that is the
model working, not failing. A customer who starts sending more invoices and fewer
letters shifts the prediction distribution without anything being wrong. Treating that
as decay would trigger a retrain every time a customer changed their paperwork mix,
and each of those retrains would pull in more review-sourced, bias-selected labels.

**I'd flip this if:** prediction drift were measured *conditional* on input segment —
a class mix that shifts within an unchanged input distribution genuinely is a model
signal. That needs input segmentation this does not have.

## The drift job is a scheduled Lambda, not a Processing job

**Chose:** a Lambda on an EventBridge schedule.

**Over:** a SageMaker Processing job, which is what the milestone brief suggests first.

**Because:** the computation is histogram arithmetic over a bounded window — a few
hundred kilobytes of counts. A Processing job would spend more on container startup
than on the work, and its only real advantage is arbitrary scale, which is not needed
until a window exceeds Lambda's memory.

**I'd flip this at a stated threshold:** when a single window no longer fits in Lambda
memory, or when drift needs embedding-based distances (which means loading a model and
GPU-adjacent compute). Both are real triggers rather than "when it feels big".

## The retrain state machine stops at registration and has no deploy path

**Chose:** `train → evaluate → gate → register(PendingManualApproval) → notify`, ending
in a `Succeed`. Approval happens in the SageMaker console; an EventBridge rule on the
approval event triggers the M2 canary deploy.

**Over:** (a) continuing to a deploy state after the gate passes; (b) using
`.waitForTaskToken` to hold the execution open until a human approves.

**Because:** (a) is the named point-loser — "retraining that automatically deploys to
production with no gate" — and a gate the same workflow can walk past is not a gate.
The approval status is *hardcoded* rather than parameterised for the same reason: as an
input, a caller could pass `Approved` and self-deploy. Two tests enforce this: one
asserting no endpoint API appears anywhere in the definition, one asserting the status
is not read from input.

(b) was tempting because it keeps the whole flow in one execution, but it couples the
retrain's lifetime to a human's calendar. Approval can legitimately take days; holding
an execution open that long to observe a console click buys nothing that an
EventBridge rule does not.

**I'd flip the human gate itself when:** there is an audited random sample of
production traffic to evaluate against (not just a frozen synthetic golden set) *and*
the canary + auto-rollback has demonstrably caught a bad version in practice. Until
both hold, the human is the only thing between "the numbers improved" and "serving
traffic".

## A rejected candidate is a successful execution

**Chose:** the gate rejecting a candidate ends in `Succeed`, and only a pipeline
failure ends in `Fail`.

**Over:** failing the execution when the candidate does not pass.

**Because:** the gate working correctly is the system functioning as designed. Marking
it as a failed execution would put a permanent error rate on the retrain state machine,
and a metric that is always red is a metric nobody reads — so the one time the
*pipeline* actually breaks, nobody notices. The two outcomes also need different
notifications: "the model did not improve" and "the training job crashed" call for
different people doing different things.

## The gate logic lives in one place, and the ASL reads its verdict

**Chose:** `evaluate.evaluate_gate` computes the pass/fail decision, writes it into
`metrics.json`, and the retrain state machine's Choice reads a single boolean.

**Over:** expressing the margin and per-class floor comparisons as ASL Choice rules.

**Because:** re-implementing the comparison in ASL creates two definitions of "better"
that can drift apart — and the one blocking releases would be the ASL copy, which has
no unit tests. It also keeps the gate testable: `TestRetrainGate` in
`tests/test_model_and_pipeline.py` exercises the collapsed-class case directly, which
would be impractical against a deployed state machine.

The same reasoning makes the retrain evaluation reuse `src.training.evaluate` as its
container entrypoint rather than a separate evaluation script. A gate comparing numbers
produced by two different code paths is comparing two different things.

---

# Decision log — M6 (CI/CD)

## Every PR check that can run without AWS, does

**Chose:** lint, type-check, tests, regression proofs, `terraform fmt`/`validate` and
the container build all run with **no credentials**. Only `terraform plan` needs AWS,
and it is skipped entirely when no deploy role is configured.

**Over:** the conventional shape, where the PR pipeline assumes an account and fails
without one.

**Because:** a check that needs cloud access does not run on forks, and it makes "is
this change correct?" depend on "is the account reachable?". A contributor should be
able to learn their change is broken without being granted an AWS role. It also means
this repo has a genuinely green CI run today, which is the only milestone here that
can say that.

The skip is deliberate rather than a workaround: `if: vars.AWS_DEPLOY_ROLE_ARN != ''`
makes the pipeline green-without-an-account rather than red-for-an-unrelated-reason.
A red pipeline that everyone knows to ignore is worse than a smaller green one.

**I'd flip this if:** the AWS-dependent checks were the ones catching real bugs. So
far the opposite is true — every bug CI has caught came from a credential-free job.

## The container is RUN in CI, not just built

**Chose:** the container job builds the image and then executes
`scripts/container_smoke.sh` against it, which starts it the way SageMaker does
(`docker run IMAGE serve`) and checks `/ping`, `/invocations`, the correlation-id
echo, the 4xx-not-5xx mapping, and 503-with-no-model.

**Over:** building the image and calling that verification, which is what most
pipelines do.

**Because:** it immediately caught a bug that a build alone never would.
`ENTRYPOINT ["gunicorn"]` with the arguments in CMD looks correct; but a command
passed to `docker run` *replaces* CMD, so SageMaker's `serve` argument became
gunicorn's module name. The container ran `gunicorn serve`, the worker died with
`ModuleNotFoundError`, and the image built perfectly. That was an M2 bug that would
have survived to a live endpoint — and the Dockerfile carried a comment claiming the
case was handled.

**The general lesson:** "it builds" and "it runs the way the platform starts it" are
different claims, and only the second one matters.

## The regression tests are proved, not asserted

**Chose:** `scripts/prove_regression_tests.py` injects five real defects into a copy
of the tree, asserts the nominated test **fails**, restores, and asserts it passes
again. It runs in CI.

**Over:** nominating a test in the README and trusting that it works.

**Because:** a test that has only ever been observed passing is an assertion about
nothing. It might be tautological, asserting on the wrong object, or silently
skipped — and all three were true here on the first run:

- a test that skipped whenever a generated artifact was absent, which is always in CI,
- an assertion that checked a condition expression *contained* `attribute_not_exists`
  and would therefore accept `attribute_exists(x) or attribute_not_exists(x)`,
- two bugs in the harness itself.

Checking both directions matters as much as the injection: a test that fails on
everything is as useless as one that fails on nothing, so the harness verifies the
test passes on clean code first.

**I'd flip this if:** mutation testing were in place — this is a hand-curated subset
of what a mutation tester does automatically. The curation is the point at this size,
though: five defects chosen because they are *plausible* and *invisible in review*
beat a thousand random mutants nobody reads.

## `main` deploys infrastructure but never promotes a model

**Chose:** the main workflow applies Terraform to dev and runs the endpoint smoke
test, but does not deploy a model version. `deploy_endpoint` stays whatever the tfvars
say, and promoting a model remains the registry-approval path.

**Over:** a pipeline where merging to main ships the newest model.

**Because:** an apply that could also swap the serving model is a deploy pipeline
pretending to be an infrastructure pipeline, and it would route around the human gate
the entire M5 design rests on. "Promote" here means "this commit passed dev and is
eligible for staging" — applying to staging stays a human action for the same reason.

`concurrency` is deliberately **not** `cancel-in-progress`: cancelling mid-apply
leaves Terraform state locked and the environment half-changed. Queueing is the only
safe behaviour for a workflow that mutates infrastructure — the opposite of the PR
workflow, where cancelling a superseded run is free.

## Plan output is redacted before it reaches a PR comment

**Chose:** `sed -E 's/[0-9]{12}/<ACCOUNT_ID>/g'` over the plan before posting, and
truncation to 60k characters.

**Over:** posting the plan verbatim, which is what the common recipes do.

**Because:** this repo is **public**. A Terraform plan routinely contains account ids
and full ARNs, and a PR comment is world-readable and permanent — it survives the
branch, the PR, and any later cleanup of the repo. The rubric names account ids in the
repo as an instant point-loser, and a bot posting them into a comment is the same
leak by a different route.

**I'd flip this if:** the repo were private and the plan were needed verbatim for
review — but even then, truncation is worth keeping, because a 60k comment is not read.
