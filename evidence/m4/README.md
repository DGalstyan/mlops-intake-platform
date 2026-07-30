# M4 evidence — observability

## What is here

`alarm-inventory.md` — **generated from the Terraform source** by
`make alarm-inventory`, with a test asserting the committed file matches a fresh
render. Not hand-maintained, and not read from `terraform output`: an output-based
inventory only exists after an apply and would describe whatever was last applied
rather than what is in the repo.

11 alarms. Each carries, in its own description, what breaks, the first response, and
a runbook link — because an alarm that fires at 3am without saying what to do has
failed at the only moment it matters.

## The distinction the inventory encodes

Every alarm has a `measures` tag classifying it as **model quality**, **system
health**, **cost**, or **data safety**. That tag is deployed config, not prose: an
earlier version of the generator inferred the classification by keyword-matching the
description and got two alarms wrong — `execution_failures` came out as "data safety"
because its text mentions the dead-letter queue.

Since model-quality-vs-system-health is precisely what this milestone is graded on,
inferring it from wording was the wrong place to be clever.

| Category | Count |
|---|---|
| model quality (proxies) | 4 |
| system health | 4 |
| cost | 1 |
| data safety | 1 |
| operational risk | 1 |

## What is missing

- **The dashboard screenshot** — half of M4's stated deliverable. The dashboard is
  defined in Terraform across four sections (business outcome first, no CPU panel
  anywhere) and validates, but a screenshot needs it deployed with real data behind it.
- **No metric has ever been emitted.** The 11 custom metrics, their dimensions, and
  every metric-math expression that derives a rate from them are unverified against
  CloudWatch. A metric-math typo renders as a blank panel and nothing local catches it.
- **No X-Ray trace captured**, and the annotation that would make the runbook's
  trace-by-`correlation_id` query work is **not implemented** — that query would
  currently return nothing.
- **Thresholds are reasoned, not calibrated.** Every one has a documented rationale;
  none has been checked against real traffic.

Regenerate with `make alarm-inventory`.
