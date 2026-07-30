variable "region" {
  description = "AWS region for the state backend. Chosen to match the main stack (Bedrock + Textract availability)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name. Prefixes the state bucket and tags every bootstrap resource."
  type        = string
  default     = "intake"
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Whether to create the GitHub Actions OIDC provider. It is an account-level
    singleton, so set this to false if your account already federates GitHub
    Actions — otherwise `terraform apply` fails with EntityAlreadyExists. The
    environment roots look the provider up by URL either way, so turning this
    off does not break the ci-deploy role.
  EOT
  type        = bool
  default     = true
}
