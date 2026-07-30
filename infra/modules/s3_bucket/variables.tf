variable "name" {
  description = "Bucket name. Must be globally unique; callers append the account id."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key used for SSE-KMS on all objects."
  type        = string
}

variable "force_destroy" {
  description = "Allow `terraform destroy` to delete the bucket even if it still has objects. True for all scratch buckets in this project."
  type        = bool
  default     = true
}

variable "expiration_days" {
  description = "Days after which current object versions expire. 0 disables expiration."
  type        = number
  default     = 0
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent (superseded) versions are permanently deleted, so versioning doesn't grow storage unbounded."
  type        = number
  default     = 30
}

variable "transitions" {
  description = "Storage-class transitions for current object versions, e.g. [{ days = 90, storage_class = \"STANDARD_IA\" }]."
  type = list(object({
    days          = number
    storage_class = string
  }))
  default = []
}

variable "abort_incomplete_multipart_upload_days" {
  description = "Days after which incomplete multipart uploads are aborted, so partial uploads don't linger as billed storage after a destroy."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
