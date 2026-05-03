variable "region" {
  description = "AWS region for the state backend resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "ai-dlc"
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}

variable "dns_zone_name" {
  description = "Public Route 53 hosted zone shared across envs. Delegate this name from the parent (e.g., smoketurner.com) using the name_servers output."
  type        = string
  default     = "aidlc.smoketurner.com"
}
