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
  description = "Visibility timeout for the state-router beacon queue. Once a beacon is received, the router has this long to read state from DDB and dispatch any side-effects before the message becomes visible again. The router intentionally does not delete the beacon on a no-op — the visibility timeout expiring is what schedules the next look at the run's state."
  type        = number
  default     = 60
}

variable "state_router_max_receives" {
  description = "Max receives before a beacon is moved to the state-router DLQ. Set high — most receives are normal polls, not consumer failures."
  type        = number
  default     = 100
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
