variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "agents" {
  description = <<-EOT
    Map of agent name → per-agent configuration. Each agent gets its own
    workload identity and AgentCore Gateway (AWS recommendation). The
    `targets` field is a subset of the registered tool Lambdas; valid
    values are `artifact_tool` and `repo_helper`.
  EOT
  type = map(object({
    description                         = string
    targets                             = set(string)
    allowed_resource_oauth2_return_urls = optional(list(string), [])
  }))
  default = {
    architect = {
      description = "Architect agent — produces ADRs."
      targets     = ["artifact_tool"]
    }
    implementer = {
      description = "Implementer agent — opens code PRs."
      targets     = ["artifact_tool", "repo_helper"]
    }
  }

  validation {
    condition = alltrue([
      for cfg in values(var.agents) :
      length(setsubtract(cfg.targets, ["artifact_tool", "repo_helper"])) == 0
    ])
    error_message = "agents[*].targets values must be a subset of {artifact_tool, repo_helper}."
  }
}

variable "memory_kms_key_arn" {
  description = "KMS key ARN that encrypts the AgentCore Memory data."
  type        = string
}

variable "tokenvault_kms_key_arn" {
  description = "KMS key ARN used as the customer master key for the AgentCore token vault."
  type        = string
}

variable "logs_kms_key_arn" {
  description = "KMS key ARN for CloudWatch log groups owned by this module (Lambda logs)."
  type        = string
}

variable "s3_kms_key_arn" {
  description = "KMS key ARN used by the artifact_tool Lambda to encrypt S3 puts."
  type        = string
}

variable "artifacts_bucket" {
  description = "S3 bucket name for run artifacts (read/write by artifact_tool)."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "S3 bucket ARN for run artifacts."
  type        = string
}

variable "memory_md_bucket" {
  description = "S3 bucket name for per-project MEMORY.md snapshots."
  type        = string
}

variable "memory_md_bucket_arn" {
  description = "S3 bucket ARN for per-project MEMORY.md snapshots."
  type        = string
}

variable "cognito_discovery_url" {
  description = "Cognito OpenID Connect discovery URL — used by the per-agent gateway JWT authorizer."
  type        = string
}

variable "cognito_audience" {
  description = "Allowed audience values (Cognito app client IDs) for the per-agent gateway JWT authorizer."
  type        = list(string)
}

variable "memory_event_expiry_days" {
  description = "Number of days after which AgentCore Memory events expire (7-365)."
  type        = number
  default     = 60
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for the tool Lambdas."
  type        = number
  default     = 30
}

variable "github_oauth" {
  description = <<-EOT
    GitHub OAuth app credentials used by the AgentCore OAuth2 credential
    provider so agents can call repo_helper with a delegated GitHub token.
    Set to `null` to skip provisioning the credential provider (no GitHub
    integration in dev). The credentials are written to AgentCore-managed
    Secrets Manager via the write-only `_wo` arguments — they never land in
    Terraform state. Bump `version` to rotate.
  EOT
  type = object({
    client_id     = string
    client_secret = string
    version       = number
  })
  default   = null
  sensitive = true
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
