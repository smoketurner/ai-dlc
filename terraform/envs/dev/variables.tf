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
  description = "OIDC callback URLs for the Cognito app client. Defaults to the dashboard's terraform-managed FQDN; override only if you need additional callbacks (e.g., a second hostname)."
  type        = list(string)
  default     = null
}

variable "dashboard_logout_urls" {
  description = "OIDC logout URLs for the Cognito app client. Defaults to the dashboard's terraform-managed FQDN root; override to add additional logout destinations."
  type        = list(string)
  default     = null
}

variable "dns_zone_name" {
  description = "Public Route 53 hosted zone the dashboard FQDN is created under. The zone itself is provisioned in terraform/bootstrap and shared across envs."
  type        = string
  default     = "aidlc.smoketurner.com"
}

variable "github_app" {
  description = <<-EOT
    GitHub App credentials. ``client_id`` + ``client_secret`` configure the
    AgentCore Identity ``GithubOauth2`` credential provider for the
    user-on-behalf-of flow; ``app_id`` + ``private_key`` (PEM) are stored
    in Secrets Manager and used by the repo_helper Lambda for installation
    tokens. Set to ``null`` to skip the GitHub integration. Bump
    ``version`` to rotate AgentCore-side credentials.
  EOT
  type = object({
    app_id        = number
    private_key   = string
    client_id     = string
    client_secret = string
    version       = number
  })
  default   = null
  sensitive = true
}

variable "aws_profile" {
  description = "AWS shared-credentials profile name. Defaults to the local SSO profile; CI sets this to \"\" so the provider falls through to env-var credentials supplied by OIDC."
  type        = string
  default     = "aidlc-admin"
}

