variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key ARN for log group encryption."
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
