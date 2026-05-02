variable "project" {
  description = "Project name (used in alias names)."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "deletion_window_in_days" {
  description = "How long the keys remain in PENDING_DELETION before destruction."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
