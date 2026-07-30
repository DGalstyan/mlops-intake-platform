variable "alias_name" {
  description = "KMS alias, e.g. alias/intake-dev."
  type        = string
}

variable "description" {
  description = "Human-readable description of what this key protects."
  type        = string
}

variable "key_admin_role_arns" {
  description = "ARNs allowed full key administration (rotate, schedule deletion, edit policy). Normally just the deploying principal/root."
  type        = list(string)
}

variable "key_user_role_arns" {
  description = "ARNs of component IAM roles allowed to Encrypt/Decrypt/GenerateDataKey with this key (e.g. training, endpoint roles)."
  type        = list(string)
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
