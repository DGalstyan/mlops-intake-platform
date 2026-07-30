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

# --- Endpoint (M2) ---------------------------------------------------------
# Only the two release decisions are surfaced here. Everything else about the
# endpoint has a justified default in infra/modules/stack.

variable "deploy_endpoint" {
  description = "Create the inference endpoint. Off by default: it is the only standing hourly cost in the stack, and it needs an Approved registry version to exist first."
  type        = bool
  default     = false
}

variable "model_package_arn" {
  description = "Approved model package version to deploy. Resolve with scripts/resolve_approved_model.py. Passing this explicitly (rather than letting Terraform re-resolve 'latest approved') means a version change shows up as a plan diff."
  type        = string
  default     = ""
}

variable "endpoint_instance_type" {
  description = "Real-time inference instance type."
  type        = string
  default     = "ml.t3.medium"
}

variable "deploy_intake" {
  description = "Create the intake orchestration (state machine, tables, Lambdas, review API)."
  type        = bool
  default     = false
}

variable "lambda_package_path" {
  description = "Path to the built Lambda zip, relative to this root. Produced by `make package-lambdas`."
  type        = string
  default     = "../../../build/intake-lambda.zip"
}

variable "bedrock_model_id" {
  description = "Bedrock model for field extraction."
  type        = string
  default     = "anthropic.claude-3-5-haiku-20241022-v1:0"
}

variable "deploy_retrain" {
  description = "Create the M5 loop: drift job, retrain state machine, approval-triggered promotion."
  type        = bool
  default     = false
}

variable "numpy_layer_arn" {
  description = "Lambda layer providing numpy for the drift job. Look up the region-specific ARN for AWSSDKPandas-Python312."
  type        = string
  default     = ""
}
