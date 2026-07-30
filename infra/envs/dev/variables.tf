variable "environment" {
  description = "Deployment environment for this root. Set in dev.tfvars, not hardcoded here."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "intake"
}

variable "github_repository" {
  description = "GitHub repository (org/repo) allowed to assume the ci-deploy role."
  type        = string
}

variable "kms_key_admin_principal_arns" {
  description = "Optional durable IAM role/user ARNs granted explicit KMS key administration. Empty means administration via the key policy's root statement plus IAM."
  type        = list(string)
  default     = []
}

variable "raw_expiration_days" {
  type    = number
  default = 30
}

variable "processed_transition_days" {
  type    = number
  default = 60
}

variable "artifacts_transition_ia_days" {
  type    = number
  default = 90
}

variable "artifacts_transition_glacier_days" {
  type    = number
  default = 365
}

variable "data_capture_expiration_days" {
  type    = number
  default = 60
}
