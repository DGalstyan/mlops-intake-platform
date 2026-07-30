output "key_arn" {
  description = "ARN of the KMS key. Consumed by every bucket/role that needs SSE-KMS."
  value       = aws_kms_key.this.arn
}

output "key_id" {
  description = "KMS key id."
  value       = aws_kms_key.this.key_id
}

output "alias_arn" {
  description = "ARN of the key's alias."
  value       = aws_kms_alias.this.arn
}
