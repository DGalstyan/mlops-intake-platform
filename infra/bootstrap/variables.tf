variable "region" {
  description = "AWS region for the state backend. Chosen to match the main stack (Bedrock + Textract availability)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project tag applied to bootstrap resources."
  type        = string
  default     = "intake"
}
