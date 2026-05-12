variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "alert_emails" {
  description = "Email addresses to subscribe to the alert SNS topic."
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the platform application group."
  type        = number
  default     = 30
}

variable "daily_token_spend_alarm_usd" {
  description = "Alarm threshold for daily token spend in USD."
  type        = number
  default     = 20
}

variable "agent_p99_latency_seconds" {
  description = "Alarm threshold for per-agent invocation p99 latency."
  type        = number
  default     = 30
}

variable "agent_error_rate_threshold" {
  description = "Alarm threshold for per-agent error rate (fraction)."
  type        = number
  default     = 0.05
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}

variable "bedrock_quota_models" {
  description = <<-EOT
    Map of friendly key -> Bedrock cross-region inference profile ID
    (e.g. ``sonnet_4_6 = "us.anthropic.claude-sonnet-4-6"``). The
    inference profile ID is the value Bedrock publishes as the
    ``ModelId`` CloudWatch dimension. Used by the per-model
    quota-usage alarms. Empty (default) skips the alarms entirely.
  EOT
  type        = map(string)
  default     = {}
}

variable "bedrock_quota_codes" {
  description = <<-EOT
    Per-model Service Quotas codes (``L-XXXXXXXX``) for the three
    Bedrock on-demand / cross-region quotas the alarms cover. Keys
    must match ``var.bedrock_quota_models``. Any sub-field left
    ``null`` skips that quota's alarms for that model. Discover the
    codes with:

        aws service-quotas list-service-quotas --service-code bedrock \
          --query "Quotas[?contains(QuotaName,'Claude')].[QuotaName,QuotaCode,Value]" \
          --output table --region us-east-1
  EOT
  type = map(object({
    tpm = optional(string)
    rpm = optional(string)
    tpd = optional(string)
  }))
  default = {}
}

variable "bedrock_quota_threshold_pct" {
  description = "Alarm thresholds expressed as a percentage of the resolved quota."
  type = object({
    warn     = number
    high     = number
    critical = number
  })
  default = {
    warn     = 50
    high     = 80
    critical = 95
  }
}
