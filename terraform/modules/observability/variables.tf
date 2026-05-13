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
    Catalog keys of Bedrock models to alarm on. Valid keys are
    defined in ``local.bedrock_quota_catalog`` (currently:
    ``opus_4_6``, ``sonnet_4_6``, ``haiku_4_5``). The catalog
    pins the CloudWatch ``ModelId`` dimension and the Service
    Quotas codes per model — both are AWS-global constants, not
    user configuration. Empty (default) skips the alarms entirely.
  EOT
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for k in var.bedrock_quota_models : contains(["opus_4_6", "sonnet_4_6", "haiku_4_5"], k)
    ])
    error_message = "bedrock_quota_models entries must be one of: opus_4_6, sonnet_4_6, haiku_4_5."
  }
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
