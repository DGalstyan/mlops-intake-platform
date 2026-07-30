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

# --- Endpoint (M2) ---------------------------------------------------------
# Off by default: the endpoint is the only resource here with a standing hourly
# cost, and it cannot exist before an Approved registry version does.

variable "deploy_endpoint" {
  description = "Whether to create the inference endpoint. Requires model_package_arn."
  type        = bool
  default     = false
}

variable "model_package_arn" {
  description = "Approved SageMaker model package version to deploy. Resolve it with scripts/resolve_approved_model.py, which refuses non-Approved versions. Ignored when deploy_endpoint is false."
  type        = string
  default     = ""

  validation {
    condition     = var.model_package_arn == "" || can(regex("^arn:aws:sagemaker:", var.model_package_arn))
    error_message = "model_package_arn must be empty or a SageMaker ARN."
  }
}

variable "endpoint_instance_type" {
  description = "Real-time inference instance type."
  type        = string
  default     = "ml.t3.medium"
}

variable "endpoint_initial_instance_count" {
  description = "Instance count at deploy time; also the autoscaling floor."
  type        = number
  default     = 1
}

variable "endpoint_data_capture_sampling_percentage" {
  description = "Percentage of requests captured. 100 in dev — M5's drift job needs a complete picture."
  type        = number
  default     = 100
}

variable "endpoint_autoscaling_max_instances" {
  description = "Scale-out ceiling, and therefore the cost ceiling."
  type        = number
  default     = 2
}

variable "endpoint_autoscaling_target_invocations" {
  description = "Target SageMakerVariantInvocationsPerInstance (per minute). Measured, not guessed: ~227/min per-instance capacity derated from scripts/measure_throughput.py, times 60% headroom. See evidence/m2/throughput.json."
  type        = number
  default     = 150
}

variable "endpoint_canary_traffic_percentage" {
  description = "Traffic share sent to the new variant in the first canary step."
  type        = number
  default     = 10
}

variable "endpoint_canary_bake_time_minutes" {
  description = "How long the canary step is held while rollback alarms are watched. Must exceed the alarm evaluation window."
  type        = number
  default     = 5
}

variable "endpoint_rollback_5xx_threshold" {
  description = "5xx count per minute that trips rollback. Low, because a canary serves little traffic."
  type        = number
  default     = 1
}

variable "endpoint_rollback_latency_threshold_ms" {
  description = "p99 ModelLatency ceiling in ms that trips rollback. 7x the measured in-process p99 of ~220ms — loose enough not to fire on jitter, tight enough to catch an unusably slow variant."
  type        = number
  default     = 1500
}

variable "alarm_sns_topic_arns" {
  description = "SNS topics notified by the rollback alarms. M4 owns the topic; empty means alarms still roll back but notify nobody."
  type        = list(string)
  default     = []
}

# --- Intake orchestration (M3) ---------------------------------------------

variable "deploy_intake" {
  description = "Create the intake state machine, its data stores, Lambdas and review API. Requires a built Lambda package (`make package-lambdas`)."
  type        = bool
  default     = false
}

variable "lambda_package_path" {
  description = "Path to the built Lambda zip, produced by `make package-lambdas`. Read as a file, so a plan with deploy_intake=true and no package fails loudly rather than deploying an empty function."
  type        = string
  default     = "../../../build/intake-lambda.zip"
}

variable "bedrock_model_id" {
  description = "Bedrock foundation model used for field extraction. A variable because 'Bedrock deprecates the version you pinned' must be a tfvars edit, not a code change."
  type        = string
  default     = "anthropic.claude-3-5-haiku-20241022-v1:0"
}

variable "enable_xray" {
  description = "Enable X-Ray tracing on the state machine and pipeline Lambdas."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for pipeline log groups."
  type        = number
  default     = 14
}
