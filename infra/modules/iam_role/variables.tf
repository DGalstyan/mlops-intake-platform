variable "role_name" {
  description = "Role name, e.g. intake-training-dev."
  type        = string
}

variable "description" {
  description = "What this role is for and which component assumes it."
  type        = string
}

variable "assume_role_policy_json" {
  description = "JSON trust policy (from an aws_iam_policy_document data source) naming the exact principal allowed to assume this role."
  type        = string
}

variable "inline_policy_json" {
  description = "JSON permission policy (from an aws_iam_policy_document data source), scoped to specific actions and resource ARNs. Set to null to create a role with no inline policy (trust-only, permissions added later)."
  type        = string
  default     = null
}

variable "tags" {
  description = "Resource tags merged with provider default_tags."
  type        = map(string)
  default     = {}
}
