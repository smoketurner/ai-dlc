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

  # Service Quotas codes are AWS-global catalog identifiers, not
  # account/region-specific — same code in every account where the
  # quota exists. `model_id` is the CloudWatch `ModelId` dimension
  # Bedrock publishes for the `us.*` cross-region inference profile.
  # `tpd` resolves to "Model invocation max tokens per day (doubled
  # for cross-region calls)" — the only daily-token quota that
  # applies to `us.*` profiles; the "Global cross-region" variants
  # cover `global.*` profiles and are deliberately not used here.
  bedrock_quota_catalog = {
    opus_4_6 = {
      model_id = "us.anthropic.claude-opus-4-6-v1"
      tpm      = "L-0AD9BBE8"
      rpm      = "L-11DFF789"
      tpd      = "L-82CD9B28"
    }
    sonnet_4_6 = {
      model_id = "us.anthropic.claude-sonnet-4-6"
      tpm      = "L-15B8E632"
      rpm      = "L-00FF3314"
      tpd      = "L-B29C9321"
    }
    haiku_4_5 = {
      model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
      tpm      = "L-58BE175A"
      rpm      = "L-CCA5DF70"
      tpd      = "L-6120CF2D"
    }
  }

  bedrock_quota_thresholds = {
    warn     = var.bedrock_quota_threshold_pct.warn
    high     = var.bedrock_quota_threshold_pct.high
    critical = var.bedrock_quota_threshold_pct.critical
  }

  bedrock_quota_lookups = {
    for pair in flatten([
      for model_key in var.bedrock_quota_models : [
        for qt in ["tpm", "rpm", "tpd"] : {
          key        = "${model_key}-${qt}"
          model_key  = model_key
          quota_type = qt
          quota_code = local.bedrock_quota_catalog[model_key][qt]
        }
      ]
    ]) : pair.key => pair
  }

  bedrock_quota_alarm_specs = {
    for spec in flatten([
      for lookup_key, lookup in local.bedrock_quota_lookups : [
        for severity, threshold_pct in local.bedrock_quota_thresholds : {
          key           = "${lookup.model_key}-${lookup.quota_type}-${severity}"
          lookup_key    = lookup_key
          model_key     = lookup.model_key
          model_id      = local.bedrock_quota_catalog[lookup.model_key].model_id
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
