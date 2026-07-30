output "state_machine_arn" {
  description = "Intake state machine ARN. Started by EventBridge on S3 upload."
  value       = aws_sfn_state_machine.intake.arn
}

output "state_machine_name" {
  description = "Intake state machine name."
  value       = aws_sfn_state_machine.intake.name
}

output "ledger_table_name" {
  description = "Idempotency ledger. One claim per bucket#key#versionId; no TTL, deliberately."
  value       = aws_dynamodb_table.ledger.name
}

output "results_table_name" {
  description = "Results store, holding both auto-approved and human-approved outcomes."
  value       = aws_dynamodb_table.results.name
}

output "review_queue_table_name" {
  description = "Human review queue. Holds task tokens, so read access is a capability."
  value       = aws_dynamodb_table.review_queue.name
}

output "corrections_table_name" {
  description = "Labelled training data from human corrections. Read by M5's retrain job."
  value       = aws_dynamodb_table.corrections.name
}

output "prompts_table_name" {
  description = "Extraction prompts, rendered from schemas/. Seed with `make seed-prompts`."
  value       = aws_dynamodb_table.prompts.name
}

output "dead_letter_queue_url" {
  description = "Dead-letter queue URL. Drain with the runbook's replay procedure."
  value       = aws_sqs_queue.dead_letter.url
}

output "dead_letter_queue_arn" {
  description = "Dead-letter queue ARN."
  value       = aws_sqs_queue.dead_letter.arn
}

output "review_api_endpoint" {
  description = "Base URL of the reviewer API. POST /reviews/corrections, SigV4-signed."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "lambda_function_names" {
  description = "The three pipeline Lambdas, each with its own least-privilege role."
  value = {
    normalize_ocr = aws_lambda_function.normalize_ocr.function_name
    validate      = aws_lambda_function.validate.function_name
    review_api    = aws_lambda_function.review_api.function_name
  }
}

output "alarm_names" {
  description = "Alarms this module owns. M4 attaches SNS actions."
  value       = [aws_cloudwatch_metric_alarm.dead_letter_not_empty.alarm_name]
}
