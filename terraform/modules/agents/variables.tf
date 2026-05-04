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
    workload identity, AgentCore Gateway, and AgentCore Runtime backed by an
    ECR image (always pulled by `:latest`). The `targets` field is a subset
    of the registered tool Lambdas; valid values are `artifact_tool` and
    `repo_helper`. Image deploys happen via the images-build workflow's
    update-agent-runtime call — terraform doesn't track image SHAs.
  EOT
  type = map(object({
    description                         = string
    targets                             = set(string)
    allowed_resource_oauth2_return_urls = optional(list(string), [])
    bedrock_model_id                    = optional(string, "")
  }))
  default = {
    architect = {
      description      = "Architect agent — writes the spec bundle (requirements + design + tasks)."
      targets          = ["artifact_tool"]
      bedrock_model_id = "us.anthropic.claude-opus-4-7-20260301-v1:0"
    }
    critic = {
      description      = "Critic agent — adversarially reviews the spec (advisory)."
      targets          = ["artifact_tool"]
      bedrock_model_id = "us.anthropic.claude-opus-4-7-20260301-v1:0"
    }
    implementer = {
      description      = "Implementer agent — works the tasks list one PR at a time."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6-20260301-v1:0"
    }
    reviewer = {
      description      = "Reviewer agent — code-reviews each task PR (advisory)."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6-20260301-v1:0"
    }
    tester = {
      description      = "Tester agent — flags test gaps in each task PR (advisory)."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
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

variable "ecr_repository_urls" {
  description = "Map of agent name → ECR repository URL (from the registry module)."
  type        = map(string)
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

variable "github_app" {
  description = <<-EOT
    GitHub App credentials used by the platform's two GitHub auth paths:

      * ``client_id`` + ``client_secret`` configure the AgentCore Identity
        ``GithubOauth2`` credential provider for the user-on-behalf-of
        flow (``USER_FEDERATION``). The user authorizes the App once via
        the dashboard's "Connect GitHub" link; AgentCore caches the
        resulting OAuth token in the Token Vault. Written via write-only
        ``_wo`` arguments — they never land in Terraform state.
      * ``app_id`` + ``private_key`` (PEM-encoded RSA) are stored in a
        platform-owned Secrets Manager secret and used by the repo_helper
        Lambda to mint installation tokens for bot operations / fallback.

    Set to ``null`` to skip the integration entirely (no GitHub access).
    Bump ``version`` to rotate the AgentCore-side credentials.
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

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
