variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "mfa_configuration" {
  description = "Cognito MFA mode (OFF | OPTIONAL | ON)."
  type        = string
  default     = "OPTIONAL"
}

variable "callback_urls" {
  description = "OIDC callback URLs allowed by the app client."
  type        = list(string)
  default     = []
}

variable "logout_urls" {
  description = "OIDC logout URLs allowed by the app client."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
