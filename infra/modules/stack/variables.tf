variable "environment" {
  description = "Deployment environment. Drives every resource name (intake-<component>-<environment>) — never hardcode dev/staging elsewhere."
  type        = string
  validation {
    condition     = contains(["dev", "staging"], var.environment)
    error_message = "environment must be \"dev\" or \"staging\"."
  }
}

variable "region" {
  description = "AWS region (must match the provider's configured region, used for ARN construction)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name, used as the resource-name prefix (intake-<component>-<env>)."
  type        = string
  default     = "intake"
}

variable "github_repository" {
  description = "GitHub repository allowed to assume the ci-deploy role, in \"org/repo\" form. Scopes the OIDC trust condition."
  type        = string
}

variable "kms_key_admin_principal_arns" {
  description = <<-EOT
    Optional durable IAM role/user ARNs granted explicit KMS key administration.
    Leave empty (the default) to rely on the key policy's root statement plus IAM,
    which is the AWS-recommended pattern. Must NOT contain an STS assumed-role
    session ARN (arn:aws:sts::...:assumed-role/Role/session) — those are ephemeral.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for a in var.kms_key_admin_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/", a))])
    error_message = "Each entry must be a durable IAM role or user ARN (arn:aws:iam::<account>:role/... or :user/...), not an STS session ARN."
  }
}

# --- Lifecycle windows (see docs/decisions.md for the rationale) -----------

variable "raw_expiration_days" {
  description = "Raw uploaded documents expire after this many days — they exist only to feed the intake pipeline and are not the long-term record."
  type        = number
  default     = 30
}

variable "processed_transition_days" {
  description = "Processed (OCR'd/normalized) documents transition to STANDARD_IA after this many days. No expiration: processed output feeds training/audit."
  type        = number
  default     = 60
}

variable "artifacts_transition_ia_days" {
  description = "Model artifacts transition to STANDARD_IA after this many days."
  type        = number
  default     = 90
}

variable "artifacts_transition_glacier_days" {
  description = "Model artifacts transition to GLACIER after this many days. No expiration: model lineage is kept."
  type        = number
  default     = 365
}

variable "data_capture_expiration_days" {
  description = "SageMaker endpoint data-capture records expire after this many days (the monitoring/drift-baseline retention window)."
  type        = number
  default     = 60
}
