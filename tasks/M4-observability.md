# M4 — Observability

**Owner:** `observability`  ·  **Skills:** `cloudwatch-observability`,
`terraform-aws-conventions`  ·  **Grade tie-in:** Observability (20%)

## Goal
A dashboard a non-engineer can read, plus an actionable alarm inventory — metrics
that map to **business outcomes, not just CPU.**

## Tasks
- [ ] Structured JSON logging with `correlation_id` propagated end-to-end
      (including into Bedrock request metadata).
- [ ] Custom CloudWatch metrics in namespace `Intake/Platform`:
      `AutoApprovalRate`, `HumanOverrideRate`, `ConfidenceP50`/`ConfidenceP10`,
      `SchemaValidationFailureRate`, `EndToEndLatencyP95` + per-stage latency,
      `LLMInputTokens`/`LLMOutputTokens`/`EstimatedCostPerDocument`.
- [ ] Dashboard **defined in Terraform** with four legible sections: model health,
      pipeline health, business outcome, cost.
- [ ] Alarms → SNS with meaningful thresholds; one README sentence each (what
      breaks, who's paged, first response); link to `docs/runbook.md`.
- [ ] X-Ray or OTel tracing across the state machine, keyed to correlation_id.
- [ ] README section: which metrics measure **model quality** vs **system health**;
      name your production accuracy **proxy** and where it misleads.

## Acceptance criteria (Deliverable)
- [ ] Dashboard screenshot + alarm inventory in `evidence/`.
- [ ] `EstimatedCostPerDocument` computed from real token counts × documented prices.
- [ ] correlation_id traceable for a single document end to end.

## Definition of done
`mlops-reviewer` confirms metrics are business/model-oriented (not CPU-only), the
dashboard is legible, alarms are actionable, and the model-vs-system-health
discussion is present.
