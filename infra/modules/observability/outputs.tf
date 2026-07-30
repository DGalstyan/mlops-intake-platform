output "alarm_topic_arn" {
  description = "SNS topic every alarm publishes to. Passed to the endpoint module so the rollback alarms notify too."
  value       = aws_sns_topic.alarms.arn
}

output "dashboard_name" {
  description = "CloudWatch dashboard name. The M4 deliverable is a screenshot of this."
  value       = aws_cloudwatch_dashboard.intake.dashboard_name
}

output "dashboard_url" {
  description = "Direct console URL for the dashboard."
  value       = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${aws_cloudwatch_dashboard.intake.dashboard_name}"
}

output "alarm_inventory" {
  description = <<-EOT
    Every alarm this module owns, with its threshold and the section of the
    model-health/system-health split it belongs to. Consumed by
    `make alarm-inventory`, which renders the inventory the M4 deliverable asks for —
    generated from the Terraform rather than hand-maintained, so it cannot drift from
    what is deployed.
  EOT
  value = concat(
    [
      {
        name      = aws_cloudwatch_metric_alarm.auto_approval_rate_dropped.alarm_name
        category  = "business outcome"
        measures  = "model quality (indirect)"
        threshold = "< ${var.auto_approval_rate_floor_percent}% over 3x15min, 2 datapoints"
      },
      {
        name      = aws_cloudwatch_metric_alarm.human_override_rate_high.alarm_name
        category  = "model health"
        measures  = "model quality (primary proxy)"
        threshold = "> ${var.human_override_rate_ceiling_percent}% of reviewed docs over 3x1h"
      },
      {
        name      = aws_cloudwatch_metric_alarm.schema_failure_rate_high.alarm_name
        category  = "model health"
        measures  = "model quality (extraction)"
        threshold = "> ${var.schema_failure_rate_ceiling_percent}% over 3x15min"
      },
      {
        name      = aws_cloudwatch_metric_alarm.confidence_p10_low.alarm_name
        category  = "model health"
        measures  = "model quality (concept-drift proxy)"
        threshold = "p10 < ${var.confidence_p10_floor} over 3x1h"
      },
      {
        name      = aws_cloudwatch_metric_alarm.cost_per_document_high.alarm_name
        category  = "cost"
        measures  = "spend"
        threshold = "> $${var.estimated_cost_per_document_ceiling_usd}/doc over 2x1h"
      },
    ],
    local.has_state_machine ? [
      {
        name      = aws_cloudwatch_metric_alarm.execution_failures[0].alarm_name
        category  = "pipeline health"
        measures  = "system health"
        threshold = ">= 1 failed execution in 5min"
      },
      {
        name      = aws_cloudwatch_metric_alarm.end_to_end_latency_high[0].alarm_name
        category  = "pipeline health"
        measures  = "system health"
        threshold = "p95 > ${var.end_to_end_latency_p95_seconds}s over 2x15min"
      },
    ] : [],
  )
}

output "metric_namespace" {
  description = "Namespace the platform's own metrics are published under."
  value       = var.metric_namespace
}
