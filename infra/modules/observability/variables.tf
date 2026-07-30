variable "name_prefix" {
  description = "Resource name prefix, e.g. \"intake-\"."
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging)."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "metric_namespace" {
  description = "CloudWatch namespace for the platform's own metrics."
  type        = string
  default     = "Intake/Platform"
}

variable "prices_file" {
  description = <<-EOT
    Path to config/prices.json. Read here rather than duplicated as Terraform
    variables so the dashboard's cost math and the Python cost estimator use the same
    numbers. Duplicated constants that must agree are the failure mode that made
    three separate wildcard counts go stale in this repo.
  EOT
  type        = string
}

variable "state_machine_name" {
  description = "Intake state machine name, for AWS/States metrics. Empty when intake is not deployed."
  type        = string
  default     = ""
}

variable "endpoint_name" {
  description = "SageMaker endpoint name, for AWS/SageMaker metrics. Empty when not deployed."
  type        = string
  default     = ""
}

variable "dead_letter_queue_name" {
  description = "Intake dead-letter queue name, for AWS/SQS metrics. Empty when not deployed."
  type        = string
  default     = ""
}

variable "review_queue_table_name" {
  description = "Review queue table name, used to surface the review backlog."
  type        = string
  default     = ""
}

variable "alarm_email" {
  description = <<-EOT
    Optional email address subscribed to the alarm topic. Left empty by default
    because a Terraform-created email subscription requires the recipient to click a
    confirmation link — an unconfirmed subscription looks configured and silently
    delivers nothing, which is worse than an obviously-empty topic.
  EOT
  type        = string
  default     = ""
}

# --- Alarm thresholds ------------------------------------------------------
# Every threshold below is a judgement with a stated reason. A placeholder threshold
# is worse than no alarm: it either never fires or fires constantly, and both teach
# people to ignore it.

variable "auto_approval_rate_floor_percent" {
  description = <<-EOT
    Alarm if the auto-approval rate falls below this. A collapse here means either the
    model got worse or the input distribution moved — it is the single most useful
    business-level early warning, because it moves before anyone complains.

    Set relative to the measured baseline: the M1 model auto-approves ~88% of golden
    documents at the 0.80 confidence threshold, so 70% is a large, unambiguous drop
    rather than normal variance.
  EOT
  type        = number
  default     = 70
}

variable "human_override_rate_ceiling_percent" {
  description = <<-EOT
    Alarm if reviewers are overriding more than this share of what they see. This is
    the primary production model-quality proxy, so it is the closest thing here to an
    "accuracy fell" alarm.

    30% is deliberately loose. Reviewers only see low-confidence and always-review
    documents, so a high override rate on that slice is NORMAL — the slice is
    selected for being hard. Alarming at, say, 10% would fire permanently. See the
    README's sampling-bias discussion.
  EOT
  type        = number
  default     = 30
}

variable "schema_failure_rate_ceiling_percent" {
  description = <<-EOT
    Alarm if extraction output fails validation more often than this. Points at the
    extraction model or a changed document layout, NOT at pipeline health — the
    pipeline is working correctly when this fires.
  EOT
  type        = number
  default     = 15
}

variable "confidence_p10_floor" {
  description = <<-EOT
    Alarm if the 10th-percentile confidence falls below this. Catches confidence
    DECAY, which is the concept-drift proxy: if p10 sinks while inputs and predicted
    classes look unchanged, the world moved in a way the features do not capture.

    p10 rather than p50 because the tail moves first — by the time the median sags,
    a large share of traffic is already being routed to humans.
  EOT
  type        = number
  default     = 0.35
}

variable "end_to_end_latency_p95_seconds" {
  description = <<-EOT
    Alarm if p95 end-to-end execution time exceeds this. Sized against the sum of the
    per-stage costs (Textract seconds + endpoint ~0.2s + Bedrock a few seconds), with
    generous headroom for retries. Deliberately excludes documents that waited for a
    human, which would otherwise dominate the statistic — see the alarm's comment.
  EOT
  type        = number
  default     = 120
}

variable "estimated_cost_per_document_ceiling_usd" {
  description = <<-EOT
    Alarm if the estimated per-document cost exceeds this. The cheapest possible
    early warning for the "cost has doubled" scenario: a runaway retry loop, a
    prompt that grew, or a model swap to a more expensive one all show up here first.
  EOT
  type        = number
  default     = 0.02
}

variable "review_age_warning_hours" {
  description = <<-EOT
    Alarm when the oldest pending review has waited this long. Review tasks expire at
    7 days and then dead-letter, so 48h leaves ample room to react before anything is
    lost. Measured as an AGE rather than a queue depth because expiry is a deadline:
    a hundred tasks queued this morning are fine, one task queued six days ago is not.
  EOT
  type        = number
  default     = 48
}

variable "review_backlog_ceiling" {
  description = <<-EOT
    Alarm if this many documents are waiting for human review. A backlog is not a
    system failure — but review tasks expire after 7 days and then dead-letter, so a
    backlog that is never drained becomes data loss on a timer.
  EOT
  type        = number
  default     = 100
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
