variable "name_prefix" {
  type = string
}

variable "environment" {
  type = string
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "artifacts_bucket_name" {
  description = "Bucket holding model artifacts, evaluation output and drift reports."
  type        = string
}

variable "artifacts_bucket_arn" {
  type = string
}

variable "data_capture_bucket_name" {
  description = "Bucket receiving endpoint data capture — the drift job's only input."
  type        = string
}

variable "data_capture_bucket_arn" {
  type = string
}

variable "training_role_arn" {
  description = "Role the retrain training and evaluation jobs assume."
  type        = string
}

variable "training_role_name" {
  type = string
}

variable "endpoint_role_arn" {
  description = "Role a promoted model runs as."
  type        = string
}

variable "model_package_group_name" {
  type = string
}

variable "alarm_topic_arn" {
  type = string
}

variable "lambda_package_path" {
  type = string
}

variable "endpoint_name" {
  description = "Endpoint the promote state machine updates. Empty disables promotion."
  type        = string
  default     = ""
}

variable "five_xx_alarm_name" {
  description = "Rollback alarm names, passed through so the promotion's DeploymentConfig names the SAME alarms Terraform attached to the endpoint. Re-declaring them here would create a second definition of 'this deployment is going wrong'."
  type        = string
  default     = ""
}

variable "latency_alarm_name" {
  type    = string
  default = ""
}

variable "training_image_uri" {
  description = "Image for the retrain training and evaluation jobs. Defaults to the inference image: a candidate evaluated in a different environment than it serves in is not evaluating the thing that will run."
  type        = string
  default     = ""
}

variable "inference_image_uri" {
  type    = string
  default = ""
}

variable "numpy_layer_arn" {
  description = <<-EOT
    Lambda layer providing numpy for the drift job. The drift math is vectorised, and
    the pipeline Lambda package deliberately carries no third-party dependencies.

    AWS's managed SDK-for-pandas layer bundles numpy and is the intended value. The
    version suffix DIFFERS BY REGION and by release, so there is no safe default —
    look it up before applying:

      aws lambda list-layer-versions --layer-name AWSSDKPandas-Python312 \
        --region <region> --query 'LayerVersions[0].LayerVersionArn' --output text

    Empty means no layer, and the function fails on `import numpy` at cold start.
    That failure is loud and immediate rather than silent, which is the right trade
    for a value that cannot be defaulted correctly.

    Worth reconsidering if it proves brittle: the window is a few hundred kilobytes of
    histogram arithmetic, so numpy is a convenience rather than a requirement, and a
    pure-Python rewrite would remove the layer entirely.
  EOT
  type        = string
  default     = ""
}

variable "drift_schedule_expression" {
  description = "How often the drift job runs. Daily, because drift is a trend: an hourly window at low volume is mostly sampling noise, and a report that cries wolf is a report nobody reads."
  type        = string
  default     = "rate(1 day)"
}

variable "baseline_s3_key" {
  description = "Key of the baseline statistics artifact within the artifacts bucket."
  type        = string
  default     = "models/current/baseline_statistics.json"
}

variable "training_instance_type" {
  type    = string
  default = "ml.m5.large"
}

variable "evaluation_instance_type" {
  type    = string
  default = "ml.m5.large"
}

variable "endpoint_instance_type" {
  type    = string
  default = "ml.t3.medium"
}

variable "endpoint_instance_count" {
  type    = number
  default = 1
}

variable "data_capture_sampling_percentage" {
  description = "Repeated on the promoted endpoint config. An endpoint config is immutable, so a promotion that omitted data capture would silently turn off the input to drift detection."
  type        = number
  default     = 100
}

variable "canary_traffic_percentage" {
  type    = number
  default = 10
}

variable "canary_bake_time_minutes" {
  type    = number
  default = 5
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "enable_xray" {
  type    = bool
  default = true
}

variable "git_sha" {
  description = "Commit that produced the retrain image, recorded as lineage on the registered version."
  type        = string
  default     = "unknown"
}

variable "tags" {
  type    = map(string)
  default = {}
}
