output "kms_key_arn" {
  value = module.stack.kms_key_arn
}

output "ecr_repository_url" {
  value = module.stack.ecr_repository_url
}

output "raw_bucket_name" {
  value = module.stack.raw_bucket_name
}

output "processed_bucket_name" {
  value = module.stack.processed_bucket_name
}

output "artifacts_bucket_name" {
  value = module.stack.artifacts_bucket_name
}

output "data_capture_bucket_name" {
  value = module.stack.data_capture_bucket_name
}

output "training_role_arn" {
  value = module.stack.training_role_arn
}

output "endpoint_role_arn" {
  value = module.stack.endpoint_role_arn
}

output "state_machine_role_arn" {
  value = module.stack.state_machine_role_arn
}

output "ci_deploy_role_arn" {
  value = module.stack.ci_deploy_role_arn
}
