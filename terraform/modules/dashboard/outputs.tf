output "alb_dns_name" {
  description = "ALB DNS name (use this until a custom domain is wired)."
  value       = aws_lb.this.dns_name
}

output "alb_arn" {
  description = "ALB ARN."
  value       = aws_lb.this.arn
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.this.name
}

output "task_role_arn" {
  description = "ARN of the dashboard's task IAM role."
  value       = aws_iam_role.task.arn
}

output "alb_security_group_id" {
  description = "Security group fronting the ALB."
  value       = aws_security_group.alb.id
}

output "url" {
  description = "Public URL for the dashboard. https://<fqdn> when use_https, otherwise http://<alb-dns>."
  value       = local.use_https ? "https://${var.dashboard_fqdn}" : "http://${aws_lb.this.dns_name}"
}
