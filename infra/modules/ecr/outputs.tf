output "repository_url" {
  description = "Registry URL, e.g. <account>.dkr.ecr.<region>.amazonaws.com/intake-inference-dev. Used by `docker push` and the endpoint's PrimaryContainer.Image."
  value       = aws_ecr_repository.this.repository_url
}

output "repository_arn" {
  description = "Repository ARN, for scoping ECR IAM permissions."
  value       = aws_ecr_repository.this.arn
}

output "repository_name" {
  value = aws_ecr_repository.this.name
}
