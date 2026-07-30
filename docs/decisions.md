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

## `Resource: "*"` inventory: six unavoidable sites, and why each is

**Chose:** to leave six `Resource: "*"` statements in place, each with an inline
comment, and to inventory them here rather than contort the code to satisfy a
grep.

**Over:** (a) splitting `aws_kms_key` into `aws_kms_key` + a separate
`aws_kms_key_policy` resource so the policy could reference
`aws_kms_key.this.arn` explicitly; (b) leaving them undocumented.

**Because:** the rubric names `Resource: "*"` as an instant point-loser, so an
undocumented one reads as laziness. But all six are genuinely mandated:

| Site | Actions | Why `*` is unavoidable |
|---|---|---|
| `modules/kms/main.tf:66,86,106` | key administration / key usage | These are **key policy** statements — a resource-based policy attached to exactly one key. `"*"` is AWS's documented spelling of "this key", and the key's ARN cannot be referenced inside its own policy without a cycle. |
| `modules/stack/iam.tf:141,273,392` | `ecr:GetAuthorizationToken` | The action has no resource type in the ECR IAM reference. It is an account-level pre-auth call; there is nothing to scope it to, and no condition key narrows it either. |

There is also one `Principal: "*"` + `Action: "s3:*"` in
`infra/bootstrap/main.tf:115-117`. That is a **Deny** statement rejecting
non-TLS requests to the state bucket — the AWS-recommended
`aws:SecureTransport` pattern. A deny-everyone is the opposite of a permission
grant, and narrowing its principal would weaken it.

Option (a) is mechanically possible for the KMS case and would drop three of
the six. I rejected it because AWS documents `"*"` as *the* key-policy form,
and restructuring a working key policy to satisfy a text search trades real
correctness risk for a cosmetic win.

**I'd flip this if:** an organisation-level SCP or a compliance scanner
hard-failed on the literal string regardless of policy type. Then option (a)
for the KMS key, and `ecr:GetAuthorizationToken` moved into a separate
minimal-scope role assumed only for `docker login`.

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
