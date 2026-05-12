locals {
  prefix          = "${var.project}-${var.env}"
  app_log_group   = "/${var.project}/${var.env}/app"
  alerts_topic    = "${local.prefix}-alerts"
  dashboard_name  = "${local.prefix}-overview"
  agent_namespace = "ai-dlc/agents"

  # Per-model Bedrock quota alarm wiring. `EstimatedTPMQuotaUsage` is
  # already burndown-adjusted server-side (5x for Claude 4+ output
  # tokens, cache-read excluded) per the AWS/Bedrock runtime metrics
  # spec; for RPM no dedicated metric exists so we sum `Invocations`.
  bedrock_quota_definitions = {
    tpm = { metric_name = "EstimatedTPMQuotaUsage", period = 60 }
    rpm = { metric_name = "Invocations", period = 60 }
    tpd = { metric_name = "EstimatedTPMQuotaUsage", period = 86400 }
  }

  bedrock_quota_thresholds = {
    warn     = var.bedrock_quota_threshold_pct.warn
    high     = var.bedrock_quota_threshold_pct.high
    critical = var.bedrock_quota_threshold_pct.critical
  }

  bedrock_quota_lookups = {
    for pair in flatten([
      for model_key, model_id in var.bedrock_quota_models : [
        for qt in ["tpm", "rpm", "tpd"] : {
          key        = "${model_key}-${qt}"
          model_key  = model_key
          quota_type = qt
          quota_code = try(var.bedrock_quota_codes[model_key][qt], null)
        }
      ]
    ]) : pair.key => pair
    if pair.quota_code != null && pair.quota_code != ""
  }

  bedrock_quota_alarm_specs = {
    for spec in flatten([
      for lookup_key, lookup in local.bedrock_quota_lookups : [
        for severity, threshold_pct in local.bedrock_quota_thresholds : {
          key           = "${lookup.model_key}-${lookup.quota_type}-${severity}"
          lookup_key    = lookup_key
          model_key     = lookup.model_key
          model_id      = var.bedrock_quota_models[lookup.model_key]
          quota_type    = lookup.quota_type
          severity      = severity
          threshold_pct = threshold_pct
          metric_name   = local.bedrock_quota_definitions[lookup.quota_type].metric_name
          period        = local.bedrock_quota_definitions[lookup.quota_type].period
        }
      ]
    ]) : spec.key => spec
  }
}
