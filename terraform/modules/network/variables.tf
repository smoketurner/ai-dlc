variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "vpc_cidr" {
  description = "Primary CIDR for the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "public_subnets" {
  description = "Public subnet CIDRs (one per AZ)."
  type        = list(string)
  default     = ["10.40.0.0/20", "10.40.16.0/20"]
}

variable "private_subnets" {
  description = "Private subnet CIDRs (one per AZ)."
  type        = list(string)
  default     = ["10.40.32.0/20", "10.40.48.0/20"]
}

variable "high_availability" {
  description = "Provision a NAT gateway per AZ when true; one shared NAT when false."
  type        = bool
  default     = false
}
