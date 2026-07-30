output "endpoint_name" {
  description = "Name of the real-time inference endpoint. Invoked by the intake state machine's Classify state (M3) and by the post-deploy smoke test."
  value       = aws_sagemaker_endpoint.this.name
}

output "endpoint_arn" {
  description = "ARN of the endpoint. Used to scope the state-machine role's sagemaker:InvokeEndpoint permission in M3."
  value       = aws_sagemaker_endpoint.this.arn
}

output "endpoint_config_name" {
  description = "Current endpoint configuration name."
  value       = aws_sagemaker_endpoint_configuration.this.name
}

output "model_name" {
  description = "SageMaker model created from the approved registry version."
  value       = aws_sagemaker_model.this.name
}

output "variant_name" {
  description = "Production variant name. Needed to address the variant in CloudWatch dimensions and autoscaling."
  value       = local.variant_name
}

output "data_capture_s3_uri" {
  description = "Where data-capture records land. This is the M5 drift job's input path."
  value       = "s3://${var.data_capture_bucket_name}/endpoint-capture/${var.environment}"
}

output "rollback_alarm_names" {
  description = "Alarms wired into auto_rollback_configuration. A deployment that breaches either of these is rolled back automatically."
  value = [
    aws_cloudwatch_metric_alarm.invocation_5xx.alarm_name,
    aws_cloudwatch_metric_alarm.model_latency.alarm_name,
  ]
}

output "autoscaling_target_resource_id" {
  description = "Application Auto Scaling resource id for the variant."
  value       = aws_appautoscaling_target.variant.resource_id
}
