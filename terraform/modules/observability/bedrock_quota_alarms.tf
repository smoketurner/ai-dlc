################################################################################
# Per-model Bedrock quota-usage alarms. Three quota dimensions (TPM, RPM,
# daily TPM) x three severities (warn/high/critical) per configured model.
# The metric expression divides current usage by the quota value resolved
# in data.tf and alarms when the percentage crosses the threshold.
################################################################################

resource "aws_cloudwatch_metric_alarm" "bedrock_quota" {
  for_each = local.bedrock_quota_alarm_specs

  alarm_name        = "${local.prefix}-bedrock-${each.value.model_key}-${each.value.quota_type}-${each.value.severity}"
  alarm_description = "Bedrock ${upper(each.value.quota_type)} usage for ${each.value.model_id} reached ${each.value.threshold_pct}% of the account quota."

  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = each.value.threshold_pct
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "usage"
    return_data = false

    metric {
      metric_name = each.value.metric_name
      namespace   = "AWS/Bedrock"
      period      = each.value.period
      stat        = "Sum"
      dimensions  = { ModelId = each.value.model_id }
    }
  }

  metric_query {
    id          = "pct"
    expression  = "usage / ${data.aws_servicequotas_service_quota.bedrock[each.value.lookup_key].value} * 100"
    label       = "% of quota"
    return_data = true
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-bedrock-${each.value.model_key}-${each.value.quota_type}-${each.value.severity}"
    Component = "observability"
  })
}
