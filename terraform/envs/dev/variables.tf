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

variable "github_owner" {
  description = "GitHub org/user that owns the source repository (used by the OIDC trust policies)."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name."
  type        = string
}

variable "alert_emails" {
  description = "Email addresses subscribed to the alerts SNS topic."
  type        = list(string)
  default     = []
}

variable "daily_token_spend_alarm_usd" {
  description = "Threshold for the daily Bedrock token-spend alarm."
  type        = number
  default     = 20
}

variable "dashboard_callback_urls" {
  description = "OIDC callback URLs for the Cognito app client. Populated once the dashboard ALB exists in Phase 7."
  type        = list(string)
  default     = []
}

variable "dashboard_logout_urls" {
  description = "OIDC logout URLs for the Cognito app client. Populated once the dashboard ALB exists in Phase 7."
  type        = list(string)
  default     = []
}

variable "github_oauth" {
  description = <<-EOT
    GitHub OAuth app credentials used by the AgentCore OAuth2 credential
    provider. Set to `null` to skip provisioning. The credentials are passed
    via Terraform write-only attributes — they never land in state.
  EOT
  type = object({
    client_id     = string
    client_secret = string
    version       = number
  })
  default   = null
  sensitive = true
}

variable "architect_image_tag" {
  description = "ECR image tag for the architect agent. Empty string skips runtime provisioning (use on first apply)."
  type        = string
  default     = ""
}

variable "implementer_image_tag" {
  description = "ECR image tag for the implementer agent. Empty string skips runtime provisioning."
  type        = string
  default     = ""
}

variable "dashboard_image_tag" {
  description = "ECR image tag for the dashboard. Empty string keeps the ECS service unprovisioned."
  type        = string
  default     = ""
}

variable "dashboard_acm_certificate_arn" {
  description = "ACM certificate ARN for the dashboard ALB HTTPS listener. Null = HTTP-only (dev only)."
  type        = string
  default     = null
}
