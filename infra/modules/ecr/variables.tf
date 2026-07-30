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

variable "tagged_image_retain_count" {
  description = "How many images to keep. Tags are immutable, so every build adds one permanently without this cap. 10 keeps several rollback targets available."
  type        = number
  default     = 10
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
