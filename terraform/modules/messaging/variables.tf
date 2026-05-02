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
  description = "KMS key ARN for SSE on the bus, archive, and queues."
  type        = string
}

variable "archive_retention_days" {
  description = "Days to retain events in the EventBridge archive."
  type        = number
  default     = 90
}

variable "hitl_visibility_seconds" {
  description = "Visibility timeout for the HITL approvals queue."
  type        = number
  default     = 60
}

variable "hitl_max_receives" {
  description = "Max receives before a message is moved to the HITL DLQ."
  type        = number
  default     = 5
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
