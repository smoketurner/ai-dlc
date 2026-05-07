variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "archive_retention_days" {
  description = "Days to retain events in the EventBridge archive."
  type        = number
  default     = 90
}

variable "state_router_visibility_seconds" {
  description = "Visibility timeout for the state-router beacon queue. Lambda receives a beacon, dispatches whatever the run's current state requires, then reports it as a batch-item failure so SQS keeps the beacon visible after this many seconds — that's how the state machine ticks. Lower = faster reactivity to agent / webhook events; higher = fewer Lambda invocations per active run."
  type        = number
  default     = 60
}

variable "state_router_max_receives" {
  description = "Max receives before a beacon is moved to the state-router DLQ. Each active beacon cycles once per visibility timeout; max_receives × visibility = wall-clock cap before DLQ. The default (10000 × 60s ≈ 7 days) covers typical spec-PR human-merge waits; the stuck-run detector re-injects beacons for runs that exceed this."
  type        = number
  default     = 10000
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
