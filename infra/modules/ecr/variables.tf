variable "name" {
  description = "Repository name, e.g. intake-inference-dev."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key used to encrypt images at rest."
  type        = string
}

variable "untagged_image_expiry_days" {
  description = "Days after which untagged images are expired by the repository lifecycle policy."
  type        = number
  default     = 14
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
