output "drift_function_name" {
  description = "Scheduled drift detection function."
  value       = aws_lambda_function.drift.function_name
}

output "drift_schedule_rule_name" {
  value = aws_cloudwatch_event_rule.drift_schedule.name
}

output "retrain_state_machine_arn" {
  description = "Retrain state machine. Trains, evaluates, gates and REGISTERS — it has no path to production, enforced both by its definition and by its IAM policy."
  value       = aws_sfn_state_machine.retrain.arn
}

output "retrain_state_machine_name" {
  value = aws_sfn_state_machine.retrain.name
}

output "promote_state_machine_arn" {
  description = "Promote state machine, or null when no endpoint is deployed. Startable ONLY by the registry-approval EventBridge rule."
  value       = one(aws_sfn_state_machine.promote[*].arn)
}

output "model_approved_rule_name" {
  description = "The rule connecting a human's approval to a canary deploy."
  value       = one(aws_cloudwatch_event_rule.model_approved[*].name)
}

output "drift_report_s3_prefix" {
  value = "s3://${var.artifacts_bucket_name}/drift-reports/"
}

output "alarm_names" {
  value = [aws_cloudwatch_metric_alarm.drift_job_failing.alarm_name]
}
