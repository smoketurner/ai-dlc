output "repository_urls" {
  description = "Map of repository key -> repository URL."
  value       = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}

output "repository_arns" {
  description = "Map of repository key -> repository ARN."
  value       = { for k, v in aws_ecr_repository.this : k => v.arn }
}

output "repository_names" {
  description = "Map of repository key -> repository name."
  value       = { for k, v in aws_ecr_repository.this : k => v.name }
}
