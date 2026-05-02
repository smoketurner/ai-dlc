################################################################################
# Baseline alarms. Per-agent and per-Lambda alarms get added by their owning
# modules in later phases.
################################################################################

resource "aws_cloudwatch_metric_alarm" "daily_token_spend" {
  alarm_name          = "${local.prefix}-daily-token-spend"
  alarm_description   = "Daily Bedrock token spend exceeded budget."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 86400
  statistic           = "Maximum"
  threshold           = var.daily_token_spend_alarm_usd
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    Currency    = "USD"
    ServiceName = "AmazonBedrock"
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-daily-token-spend"
    Component = "observability"
  })
}
