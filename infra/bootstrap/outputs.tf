output "state_bucket_name" {
  description = "S3 bucket holding remote Terraform state for all environments. Consumed by `make init` to configure each env root's backend."
  value       = aws_s3_bucket.state.id
}

output "state_bucket_arn" {
  description = "ARN of the remote state bucket."
  value       = aws_s3_bucket.state.arn
}

output "github_oidc_provider_arn" {
  description = "ARN of the account's GitHub Actions OIDC provider, referenced (by deterministic ARN, not this output) by the ci-deploy role trust policy in infra/modules/stack."
  value       = aws_iam_openid_connect_provider.github_actions.arn
}

output "region" {
  description = "Region the state backend lives in."
  value       = var.region
}
