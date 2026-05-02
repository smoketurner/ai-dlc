variable "project" {
  description = "Project name applied to every resource."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name (dev | prod)."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Additional tags merged into the provider's default_tags."
  type        = map(string)
  default     = {}
}
