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

variable "execution_role_arn" {
  description = "SageMaker endpoint execution role ARN (component=endpoint)."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key for endpoint storage and data-capture encryption."
  type        = string
}

variable "data_capture_bucket_name" {
  description = "Bucket receiving data-capture records. Read by the M5 drift job."
  type        = string
}

variable "model_package_arn" {
  description = <<-EOT
    ARN of the SageMaker Model Package version to deploy. Must be an **Approved**
    version — resolved by scripts/resolve_approved_model.py, which refuses
    anything else. Deliberately a required variable with no default rather than a
    lookup inside Terraform: "which version is live" is a release decision that
    should appear in a plan diff, not something a plan silently re-resolves.
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:aws:sagemaker:[a-z0-9-]+:[0-9]{12}:model-package/", var.model_package_arn))
    error_message = "model_package_arn must be a SageMaker model-package ARN."
  }
}

variable "instance_type" {
  description = <<-EOT
    Real-time inference instance type. ml.t3.medium is the smallest that fits the
    TF-IDF model comfortably and keeps a full graded run inside the ~$15 budget
    (~$0.05/hour). The model is a sparse dot product; it is not compute-bound.
  EOT
  type        = string
  default     = "ml.t3.medium"
}

variable "initial_instance_count" {
  description = "Instances at deploy time. Autoscaling adjusts from here."
  type        = number
  default     = 1
}

variable "data_capture_sampling_percentage" {
  description = <<-EOT
    Percentage of requests captured. 100 in dev: the M5 drift job needs a complete
    picture, and at dev traffic volumes full capture costs nothing measurable.
    Lower this in an environment with real traffic, where storage and the
    downstream Processing job scan both scale with it.
  EOT
  type        = number
  default     = 100

  validation {
    condition     = var.data_capture_sampling_percentage > 0 && var.data_capture_sampling_percentage <= 100
    error_message = "data_capture_sampling_percentage must be in (0, 100]."
  }
}

# --- Autoscaling -----------------------------------------------------------

variable "autoscaling_max_instances" {
  description = "Upper bound on scale-out. Also the cost ceiling."
  type        = number
  default     = 2
}

variable "autoscaling_target_invocations_per_instance" {
  description = <<-EOT
    Target for SageMakerVariantInvocationsPerInstance, in invocations per minute
    per instance.

    Measured, not guessed. scripts/measure_throughput.py against the real handler
    path gives ~650 invocations/minute on a developer laptop at p99 220ms. Derated
    by 0.35 for an ml.t3.medium's 2 burstable vCPUs gives ~227/minute of real
    per-instance capacity; 60% of that is the target, rounded to 150.

    The headroom is the point: target tracking is a steady-state signal and
    bringing a new instance into service takes minutes, so a target set *at*
    capacity means the endpoint is already queueing before help arrives.

    An earlier revision of this file guessed 900 here. That is 4x measured
    capacity — the policy would effectively never have scaled out. See
    evidence/m2/throughput.json.
  EOT
  type        = number
  default     = 150
}

variable "autoscaling_scale_in_cooldown_seconds" {
  description = <<-EOT
    Longer than scale-out on purpose. Removing an instance during a lull that
    turns out to be a gap between bursts causes a latency spike on the next burst,
    so scale-in is the direction that should hesitate.
  EOT
  type        = number
  default     = 300
}

variable "autoscaling_scale_out_cooldown_seconds" {
  description = "Shorter than scale-in: responding late to load is the worse failure."
  type        = number
  default     = 60
}

# --- Rollback alarms -------------------------------------------------------

variable "rollback_5xx_threshold" {
  description = <<-EOT
    Number of ModelInvocation5XXErrors in a one-minute period that trips the
    rollback. Deliberately low: during a canary only a small share of traffic
    reaches the new variant, so a broken variant produces few absolute errors. A
    threshold tuned for full traffic would never fire during the canary phase,
    which is exactly when it needs to.
  EOT
  type        = number
  default     = 1
}

variable "rollback_latency_threshold_ms" {
  description = <<-EOT
    ModelLatency p99 ceiling in milliseconds that trips the rollback.

    7x the measured in-process p99 of ~220ms (evidence/m2/throughput.json). Catches
    a variant that is functional but unusably slow — the failure a pure error-rate
    alarm never sees, because a slow endpoint returns 200s.

    Sized this way deliberately: an alarm that fires on ordinary jitter or a cold
    start is worse than no alarm, because a guardrail that cries wolf gets
    disabled. The measured p99 excludes HTTP framing and SageMaker overhead, so the
    real p99 will be higher and the effective multiple smaller than 7x — replace
    this with a threshold derived from real endpoint metrics once any traffic has
    been served.
  EOT
  type        = number
  default     = 1500
}

variable "canary_traffic_percentage" {
  description = <<-EOT
    Share of traffic sent to the new variant in the first canary step. 10% is
    small enough to bound blast radius and large enough that a broken variant
    generates alarm-visible errors within the baking period.
  EOT
  type        = number
  default     = 10
}

variable "canary_bake_time_minutes" {
  description = <<-EOT
    How long the canary step is held while alarms are watched before shifting the
    remaining traffic. Must exceed the alarm's evaluation window (period x
    evaluation_periods) or the deployment can complete before the alarm has had
    enough datapoints to fire — a rollback that exists on paper and never triggers.
  EOT
  type        = number
  default     = 5
}

variable "alarm_sns_topic_arns" {
  description = "SNS topics notified when a rollback alarm fires. M4 owns the topic."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
