output "state_bucket_name" {
  description = "S3 bucket holding remote Terraform state for all environments. Consumed by `make init` to configure each env root's backend."
  value       = aws_s3_bucket.state.id
}

output "state_bucket_arn" {
  description = "ARN of the remote state bucket."
  value       = aws_s3_bucket.state.arn
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC provider created by this root, or null when create_github_oidc_provider is false. The environment roots do not read this output — they look the provider up by URL with a data source, so they work whether or not this root created it."
  value       = one(aws_iam_openid_connect_provider.github_actions[*].arn)
}

output "region" {
  description = "Region the state backend lives in."
  value       = var.region
}
