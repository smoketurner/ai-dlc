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

output "state_router_queue_url" {
  value = aws_sqs_queue.state_router.url
}

output "state_router_queue_arn" {
  value = aws_sqs_queue.state_router.arn
}
