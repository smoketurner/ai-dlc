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

variable "bedrock_quota_models" {
  description = <<-EOT
    Catalog keys of Bedrock models to alarm on. Valid keys are
    defined by the observability module
    (``opus_4_6``, ``sonnet_4_6``, ``haiku_4_5``). Empty (default)
    skips all alarms.
  EOT
  type        = set(string)
  default     = []
}

variable "bedrock_quota_threshold_pct" {
  description = "Bedrock quota alarm thresholds as a percentage of the resolved quota."
  type = object({
    warn     = number
    high     = number
    critical = number
  })
  default = {
    warn     = 50
    high     = 80
    critical = 95
  }
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

variable "github_app_secret_name" {
  description = <<-EOT
    Name of the AWS Secrets Manager secret holding the GitHub App
    credentials (operator-managed; created out-of-band so terraform never
    destroys it). The secret value is a JSON object with keys ``app_id``,
    ``private_key_base64``, ``client_id``, ``client_secret``, ``version``.
    Set to ``null`` to skip the GitHub integration.
  EOT
  type        = string
  default     = null
  nullable    = true
}

variable "github_bot_login" {
  description = <<-EOT
    Login of the GitHub bot the platform runs as. The dashboard webhook
    handler routes ``issues.assigned`` to triage when the new assignee
    matches this login. Empty disables that trigger; the label-based
    (``aidlc:ready``) and ``/aidlc go``-comment paths still fire.
  EOT
  type        = string
  default     = ""
}

variable "aws_profile" {
  description = "AWS shared-credentials profile name. Defaults to the local SSO profile; CI sets this to \"\" so the provider falls through to env-var credentials supplied by OIDC."
  type        = string
  default     = "aidlc-admin"
}

