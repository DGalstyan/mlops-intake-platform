# Evidence

What each folder contains, and — more importantly — **what it is not**.

The assignment asks for five artifacts. Two exist as asked, two exist as an honest
local substitute, and one is absent. This table is the summary; each folder's own
README carries the detail.

| Deliverable | Milestone | Status |
|---|---|---|
| `terraform plan` on a clean account | M0 | **absent** — no AWS credentials |
| Two distinguishable registry versions | M1 | **real, local** — metrics are genuine; nothing was written to a Model Package Group |
| Recorded bad deploy that auto-rolled back | M2 | **absent** — the headline M2 deliverable |
| Trace of one auto-approved + one corrected document | M3 | **local simulation**, not a Step Functions execution |
| Dashboard screenshot + alarm inventory | M4 | **half** — inventory generated from Terraform; no screenshot |
| Drift report from a shifted batch | M5 | **real** — genuine math against the real baseline |
| Full retrain → gate → approve → canary cycle | M5 | **absent** |
| Green PR run + green main run | M6 | **real** — GitHub Actions actually ran these |

## The one-line version

**Nothing in this repo has ever been deployed to AWS.** No credentials were available
on the machine it was built on. Everything that could be verified without an account
has been; everything that could not is named as absent rather than approximated.

## Folder by folder

### `m1/` — training and registry
Real training output: two model versions on the same frozen golden set. v2
(uncalibrated) is **more accurate** than v1 — macro-F1 0.9543 vs 0.9417 — while its
ECE is **19× worse** (0.2622 vs 0.0140). Since routing gates on `max(predict_proba)`,
v2 would confidently auto-approve documents it should escalate while winning on every
accuracy-shaped metric.

*Not real:* no version was written to a Model Package Group. `register.py` has only
been run `--dry-run`.

### `m2/` — deployment
A real load measurement (p99 220ms, ~650 invocations/min) that corrected two of my own
guessed thresholds — the autoscaling target had been set to **4× measured capacity**,
which would have meant the policy never scaled out.

*Absent:* the recorded auto-rollback, which is M2's actual deliverable. The canary
policy and both alarms are written and validated; whether they fire is unproven.

### `m3/` — intake traces
Traces of an auto-approved document, a human-corrected document, a duplicate delivery,
and a schema failure. Real: the classifier, both Lambda handlers, the validator, the
correction flow, the routing conditions *and their evaluation order*, the ledger
semantics. Stubbed: Textract, Bedrock, DynamoDB, Step Functions itself.

`TestSimulatorMatchesAsl` asserts the simulator has not diverged from the deployed
definition — without it these traces would describe a workflow that does not exist.

### `m4/` — observability
`alarm-inventory.md`, generated from the Terraform source by `make alarm-inventory`,
with a test asserting the committed file matches a fresh render. 11 alarms, each
classified as model-quality or system-health via a deployed tag rather than inferred
from prose.

*Absent:* the dashboard screenshot. No metric has ever been emitted, so the
metric-math expressions behind every rate are unverified.

### `m5/` — drift
**The most genuinely-real evidence here.** Three scenarios against the actual M1
baseline: control → `NO_DRIFT`; shifted batch → `DATA_CHANGED` with retrain **off**
despite PSI 19.8; stable inputs with rising overrides → `MODEL_DECAYED` with retrain
**on**.

Running the control caught two false positives that would have fired forever on
unchanged data — a KS statistic measuring my own histogram reconstruction, and a
baseline that measured confidence on training data.

*Absent:* the full retrain → gate → approve → canary cycle.

### `m6/` — CI
The only evidence produced by something other than this machine. GitHub Actions ran
both workflows green. Caught three bugs local development had masked, including a
container that **could not have started on SageMaker**.

*Absent:* every AWS-dependent job is gated and skipped.

## Reproducing

```bash
make ci-local          # everything a PR runs: lint, types, tests, regression proofs, terraform
make two-versions      # M1 evidence
make measure-throughput# M2 evidence
make simulate-intake   # M3 traces
make alarm-inventory   # M4 inventory
make drift-scenarios   # M5 drift reports
make prove-regressions # M6 regression proof
```
