variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "artifacts_noncurrent_expiration_days" {
  description = "Lifecycle expiration for noncurrent versions in the artifacts bucket."
  type        = number
  default     = 365
}

variable "memory_md_noncurrent_expiration_days" {
  description = "Lifecycle expiration for noncurrent versions in the memory_md bucket."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
