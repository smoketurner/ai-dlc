output "bus_name" {
  value = aws_cloudwatch_event_bus.this.name
}

output "bus_arn" {
  value = aws_cloudwatch_event_bus.this.arn
}

output "archive_arn" {
  value = aws_cloudwatch_event_archive.this.arn
}

output "schema_registry_name" {
  value = aws_schemas_registry.this.name
}

output "schema_arns" {
  value = { for k, v in aws_schemas_schema.this : k => v.arn }
}

output "hitl_approvals_queue_url" {
  value = aws_sqs_queue.hitl_approvals.url
}

output "hitl_approvals_queue_arn" {
  value = aws_sqs_queue.hitl_approvals.arn
}

output "eventbridge_dlq_arn" {
  value = aws_sqs_queue.eventbridge_dlq.arn
}
