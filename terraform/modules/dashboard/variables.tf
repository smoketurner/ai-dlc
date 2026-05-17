variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "common_layer_arn" {
  description = "Shared Lambda layer ARN (common package + powertools + boto3 + pydantic + ...)."
  type        = string
}

variable "dashboard_fqdn" {
  description = "Public FQDN for the dashboard (e.g., dashboard-dev.aidlc.smoketurner.com). When set together with route53_zone_id, the module manages the ACM cert + DNS A-alias automatically. When null, the API Gateway execution URL is used."
  type        = string
  default     = null
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone ID for `dashboard_fqdn`. Required when dashboard_fqdn is set."
  type        = string
  default     = null
}

variable "bus_name" {
  description = "EventBridge bus name."
  type        = string
}

variable "bus_arn" {
  description = "EventBridge bus ARN."
  type        = string
}

variable "runs_table_arn" {
  description = "DynamoDB runs-table ARN."
  type        = string
}

variable "runs_table" {
  description = "DynamoDB runs-table name."
  type        = string
}

variable "idempotency_table_arn" {
  description = "DynamoDB idempotency-keys-table ARN."
  type        = string
}

variable "idempotency_table" {
  description = "DynamoDB idempotency-keys-table name."
  type        = string
}

variable "artifacts_bucket" {
  description = "Artifacts S3 bucket name (read-only access for ADR/spec presigned URLs)."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "Artifacts S3 bucket ARN."
  type        = string
}

variable "github_app_secret_arn" {
  description = <<-EOT
    Secrets Manager ARN of the GitHub App credentials. The dashboard
    reads this to mint installation tokens directly (e.g., the eyes
    reaction on a freshly-assigned issue) without going through a
    Lambda hop.
  EOT
  type        = string
}

variable "github_webhook_secret_id" {
  description = "Secrets Manager secret id holding the GitHub webhook signing secret."
  type        = string
}

variable "github_webhook_secret_arn" {
  description = "Secrets Manager secret ARN."
  type        = string
}

variable "cognito_user_pool_id" {
  description = "Cognito user pool ID."
  type        = string
}

variable "cognito_user_pool_client_id" {
  description = "Cognito user pool app client ID."
  type        = string
}

variable "cognito_user_pool_domain" {
  description = "Cognito user pool hosted-UI domain (without protocol)."
  type        = string
}

variable "cognito_client_secret_id" {
  description = "Secrets Manager secret id holding the Cognito user-pool app-client secret."
  type        = string
}

variable "cognito_client_secret_arn" {
  description = "Secrets Manager secret ARN holding the Cognito user-pool app-client secret."
  type        = string
}

variable "cognito_discovery_url" {
  description = "Cognito OIDC discovery URL (.well-known/openid-configuration)."
  type        = string
}

variable "memory_size_mb" {
  description = "Lambda memory size in MB."
  type        = number
  default     = 1024
}

variable "lambda_timeout_seconds" {
  description = "Lambda timeout. Must be ≤ 30 (API Gateway HTTP API hard limit)."
  type        = number
  default     = 29
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda + API Gateway."
  type        = number
  default     = 5
}

variable "dashboard_workload_name" {
  description = "AgentCore workload identity name for the dashboard. Empty disables /auth/github."
  type        = string
  default     = ""
}

variable "github_oauth_provider_name" {
  description = "AgentCore Identity OAuth2 credential provider name (GithubOauth2). Empty disables /auth/github."
  type        = string
  default     = ""
}

variable "dashboard_oauth_return_url" {
  description = "Absolute URL of /auth/github/callback. Passed to AgentCore as resourceOauth2ReturnUrl on GetResourceOauth2Token; must match the value in the dashboard workload identity's allowed_resource_oauth2_return_urls list."
  type        = string
  default     = ""
}

variable "github_bot_login" {
  description = "Login of the GitHub bot the platform runs as (e.g., 'aidlc-bot' or 'aidlc[bot]'). When set, an issues.assigned webhook routes to triage if the new assignee matches. Empty disables the assigned-trigger."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
