################################################################################
# SNS topic for alarm notifications.
################################################################################

resource "aws_sns_topic" "alerts" {
  name = local.alerts_topic

  tags = merge(var.tags, {
    Name      = local.alerts_topic
    Component = "observability"
  })
}

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alert_emails)

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}
