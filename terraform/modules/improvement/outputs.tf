output "telemetry_function_arn" {
  description = "Telemetry Lambda ARN."
  value       = module.telemetry.lambda_function_arn
}

output "rejections_rule_arn" {
  description = "EventBridge rule that routes rejection events to the telemetry Lambda."
  value       = aws_cloudwatch_event_rule.rejections.arn
}
