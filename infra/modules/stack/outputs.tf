# Outputs consumed by later milestones via `terraform_remote_state` (state
# key: envs/<environment>/terraform.tfstate in the bootstrap state bucket).
# Do not guess ARNs — read them from here.

output "kms_key_arn" {
  description = "Project KMS key ARN. Any new component that reads/writes an encrypted bucket needs kms:Decrypt/GenerateDataKey on this ARN added to its own role policy, and its role ARN added to the key's key_user_role_arns in infra/modules/stack/main.tf."
  value       = module.kms.key_arn
}

output "kms_key_id" {
  value = module.kms.key_id
}

output "ecr_repository_url" {
  description = "Push training/inference images here: docker push <this>:<tag>."
  value       = module.ecr.repository_url
}

output "ecr_repository_arn" {
  value = module.ecr.repository_arn
}

output "raw_bucket_name" {
  value = module.raw_bucket.bucket_name
}

output "raw_bucket_arn" {
  value = module.raw_bucket.bucket_arn
}

output "processed_bucket_name" {
  value = module.processed_bucket.bucket_name
}

output "processed_bucket_arn" {
  value = module.processed_bucket.bucket_arn
}

output "artifacts_bucket_name" {
  value = module.artifacts_bucket.bucket_name
}

output "artifacts_bucket_arn" {
  value = module.artifacts_bucket.bucket_arn
}

output "data_capture_bucket_name" {
  value = module.data_capture_bucket.bucket_name
}

output "data_capture_bucket_arn" {
  value = module.data_capture_bucket.bucket_arn
}

output "training_role_arn" {
  description = "Attach to the SageMaker Training Job. Extend its inline policy in infra/modules/stack/iam.tf if a future milestone needs a new action."
  value       = module.training_role.role_arn
}

output "endpoint_role_arn" {
  description = "Attach to the SageMaker endpoint config's ExecutionRoleArn."
  value       = module.endpoint_role.role_arn
}

output "state_machine_role_arn" {
  description = "Attach to the Step Functions state machine. Trust-only at M0 — see iam.tf header comment; M3/M5 add task-invoke permissions here."
  value       = module.state_machine_role.role_arn
}

output "ci_deploy_role_arn" {
  description = "GitHub Actions assumes this via OIDC (role-to-assume in the workflow's permissions: id-token: write step). Scoped to this environment only."
  value       = module.ci_deploy_role.role_arn
}

# --- Endpoint (M2). Null when deploy_endpoint is false. --------------------

output "endpoint_name" {
  description = "Real-time inference endpoint name, or null when the endpoint is not deployed. Consumed by the smoke test and by M3's Classify state."
  value       = one(module.endpoint[*].endpoint_name)
}

output "endpoint_arn" {
  description = "Endpoint ARN, or null. M3 scopes sagemaker:InvokeEndpoint to this."
  value       = one(module.endpoint[*].endpoint_arn)
}

output "endpoint_data_capture_s3_uri" {
  description = "Where data-capture records land — the M5 drift job's input path."
  value       = one(module.endpoint[*].data_capture_s3_uri)
}

output "endpoint_rollback_alarm_names" {
  description = "Alarms wired into the endpoint's auto_rollback_configuration."
  value       = try(module.endpoint[0].rollback_alarm_names, [])
}
