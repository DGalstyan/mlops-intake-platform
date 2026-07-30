variable "name_prefix" {
  description = "Resource name prefix, e.g. \"intake-\"."
  type        = string
}

variable "environment" {
  description = "Environment name (dev, staging)."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "account_id" {
  description = "AWS account id, from data.aws_caller_identity in the caller."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key used for DynamoDB, SQS, and CloudWatch Logs encryption."
  type        = string
}

variable "raw_bucket_name" {
  description = "Bucket documents are uploaded to. The EventBridge rule watches this."
  type        = string
}

variable "raw_bucket_arn" {
  description = "ARN of the raw bucket, for scoping Textract and Lambda read access."
  type        = string
}

variable "processed_bucket_arn" {
  description = "ARN of the processed bucket."
  type        = string
}

variable "state_machine_role_name" {
  description = <<-EOT
    Name of the Step Functions execution role created in infra/modules/stack. The
    role is created there (trust-only at M0) and its permission policy is attached
    here, by the milestone that creates the resources it needs to reach. Passing the
    name rather than creating the role locally keeps one role per component, which
    is the M0 convention.
  EOT
  type        = string
}

variable "endpoint_name" {
  description = <<-EOT
    SageMaker endpoint the Classify state invokes. Empty when the endpoint is not
    deployed, in which case the state machine is still created but its
    InvokeEndpoint permission is scoped to a name that does not exist yet — the
    execution fails at Classify with an accurate error rather than the state machine
    failing to deploy.
  EOT
  type        = string
  default     = ""
}

variable "bedrock_model_id" {
  description = <<-EOT
    Bedrock model used for field extraction. A variable, not a constant, because
    "Bedrock deprecates the model version you pinned" is a scenario this design has
    to answer: changing the model is a tfvars edit plus a plan, with no code change,
    because the prompt lives in DynamoDB and the response parser tolerates more than
    one response shape.
  EOT
  type        = string
  default     = "anthropic.claude-3-5-haiku-20241022-v1:0"
}

variable "lambda_package_path" {
  description = <<-EOT
    Path to the built Lambda zip. Produced by `make package-lambdas`, which stages
    only the modules the handlers actually import — the handlers depend on nothing
    outside the standard library and boto3, so no dependency layer is needed.
  EOT
  type        = string
}

variable "lambda_runtime" {
  description = "Python runtime for the pipeline Lambdas. Matches the local venv so behaviour is the same in both places."
  type        = string
  default     = "python3.12"
}

variable "lambda_memory_mb" {
  description = <<-EOT
    Memory for the pipeline Lambdas. 512MB is well above what either handler needs;
    Lambda scales CPU with memory, and the OCR reading-order sort is CPU-bound, so
    the cheapest *total* cost is often more memory for less duration.
  EOT
  type        = number
  default     = 512
}

variable "lambda_timeout_seconds" {
  description = "Timeout for the pipeline Lambdas. Both are pure computation over a single document."
  type        = number
  default     = 30
}

variable "review_timeout_days" {
  description = <<-EOT
    How long a document waits in the review queue before the task token expires and
    the document dead-letters. Must match TimeoutSeconds on the CreateReviewTask
    state in the ASL — a shorter Terraform value would have no effect, and a longer
    one would be a lie.
  EOT
  type        = number
  default     = 7
}

variable "dead_letter_retention_days" {
  description = <<-EOT
    SQS retention for the dead-letter queue. 14 days is the AWS maximum and the
    right choice here: the queue exists to be drained by a human after
    investigation, and a failure discovered on return from a week's leave should
    still be replayable.
  EOT
  type        = number
  default     = 14
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. Short by default — logs are for debugging a recent failure, and M4's metrics are the durable record."
  type        = number
  default     = 14
}

variable "enable_xray" {
  description = "Enable X-Ray tracing on the state machine and Lambdas. M4 owns the tracing story; the switch lives here because it is a property of these resources."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
