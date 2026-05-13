output "app_log_group_name" {
  value = aws_cloudwatch_log_group.app.name
}

output "app_log_group_arn" {
  value = aws_cloudwatch_log_group.app.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.overview.dashboard_name
}

output "bedrock_quota_alarm_names" {
  description = "Map of (model, quota_type, severity) key -> alarm name for the Bedrock quota-usage alarms."
  value       = { for k, a in aws_cloudwatch_metric_alarm.bedrock_quota : k => a.alarm_name }
}
