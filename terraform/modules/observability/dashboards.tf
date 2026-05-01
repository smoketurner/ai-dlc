################################################################################
# CloudWatch dashboard. Widgets reference metrics that don't exist yet
# (per-agent invocation latency, error rate, token spend); they'll show "no
# data" until the agents and Lambdas in later phases publish them.
################################################################################

resource "aws_cloudwatch_dashboard" "overview" {
  dashboard_name = local.dashboard_name

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# ${var.project} ${var.env}\nAgent runs, latency, token spend, HITL queue depth, DLQ visibility."
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 12
        height = 6
        properties = {
          title  = "Daily Bedrock spend (USD)"
          region = ""
          metrics = [
            ["AWS/Billing", "EstimatedCharges", "ServiceName", "AmazonBedrock", "Currency", "USD"],
          ]
          stat   = "Maximum"
          period = 86400
        }
      },
    ]
  })
}
