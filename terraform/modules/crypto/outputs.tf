output "key_arns" {
  description = "Map of purpose -> KMS key ARN."
  value       = { for k, v in aws_kms_key.this : k => v.arn }
}

output "alias_arns" {
  description = "Map of purpose -> KMS alias ARN."
  value       = { for k, v in aws_kms_alias.this : k => v.arn }
}

output "alias_names" {
  description = "Map of purpose -> alias name."
  value       = { for k, v in aws_kms_alias.this : k => v.name }
}
