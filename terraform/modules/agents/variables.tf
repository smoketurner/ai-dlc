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
    `repo_helper`. The `features` field opts the agent into shared
    AgentCore compute resources; valid values are `browser` and
    `code_interpreter`. Image deploys happen via the images-build workflow's
    update-agent-runtime call — terraform doesn't track image SHAs.
  EOT
  type = map(object({
    description                         = string
    targets                             = set(string)
    features                            = optional(set(string), [])
    allowed_resource_oauth2_return_urls = optional(list(string), [])
    bedrock_model_id                    = optional(string, "")
  }))
  default = {
    architect = {
      description      = "Architect agent — writes the spec bundle (requirements + design + tasks)."
      targets          = ["artifact_tool"]
      bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
    }
    critic = {
      description      = "Critic agent — adversarially reviews the spec (advisory)."
      targets          = ["artifact_tool"]
      bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
    }
    implementer = {
      description      = "Implementer agent — works the tasks list one PR at a time."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    reviewer = {
      description      = "Reviewer agent — code-reviews each task PR (advisory)."
      targets          = ["artifact_tool", "repo_helper"]
      features         = ["code_interpreter"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    tester = {
      description      = "Tester agent — flags test gaps in each task PR (advisory)."
      targets          = ["artifact_tool", "repo_helper"]
      features         = ["code_interpreter"]
      bedrock_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
    proposer = {
      description      = "Proposer agent — schedules-driven; opens PRs proposing prompt/MEMORY edits."
      targets          = ["repo_helper"]
      features         = ["browser"]
      bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
    }
    triage = {
      description      = "Triage agent — classifies tagged GitHub issues and routes them into a workflow phase."
      targets          = []
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

  validation {
    condition = alltrue([
      for cfg in values(var.agents) :
      length(setsubtract(cfg.features, ["browser", "code_interpreter"])) == 0
    ])
    error_message = "agents[*].features values must be a subset of {browser, code_interpreter}."
  }
}

variable "ecr_repository_urls" {
  description = "Map of agent name → ECR repository URL (from the registry module)."
  type        = map(string)
}

variable "agent_image_tags" {
  description = <<-EOT
    Map of agent name → ECR image tag (typically ``"latest"``). Only agents
    listed here get an AgentCore Runtime created — agents whose image hasn't
    been pushed to ECR yet are skipped, so the IAM role + gateway + workload
    identity can be apply-able before the first image build.

    Lifecycle: add a new agent to ``var.agents``, apply (creates IAM /
    gateway / workload identity but no runtime yet), push the image via the
    ``images-build`` workflow, add the agent to this map, apply again
    (creates the runtime). Subsequent image pushes flow through the
    workflow's ``update-agent-runtime`` call — Terraform ignores the
    container URI on subsequent applies via the runtime resource's
    ``lifecycle.ignore_changes``.
  EOT
  type        = map(string)
  default     = {}
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

variable "dashboard_oauth_return_url" {
  description = <<-EOT
    Absolute URL of the dashboard's ``/auth/github/callback`` route.
    AgentCore Identity redirects users here after they finish authorizing
    the GitHub App. Must be in the dashboard workload identity's
    ``allowed_resource_oauth2_return_urls`` (this module sets it for you)
    AND passed by the dashboard as ``resourceOauth2ReturnUrl`` to
    ``GetResourceOauth2Token`` (this module exports it for plumbing).
    Empty string disables the OBO flow.
  EOT
  type        = string
  default     = ""
}

variable "github_app_secret_name" {
  description = <<-EOT
    Name of an AWS Secrets Manager secret holding the GitHub App credentials,
    in JSON shape:

        {
          "app_id":             3598242,
          "private_key_base64": "<base64 of the App's PEM private key>",
          "client_id":          "Iv23li...",
          "client_secret":      "...",
          "version":            1
        }

    The secret is **operator-managed** (created out-of-band, e.g. via the
    AWS console or one-shot ``aws secretsmanager create-secret`` call) so
    that ``terraform apply`` runs in environments without access to the
    raw values (CI/CD) won't destroy the integration. Terraform reads the
    secret via a data source and uses:

      * ``client_id`` + ``client_secret`` + ``version`` to configure the
        AgentCore Identity ``GithubOauth2`` credential provider for the
        user-on-behalf-of flow (``USER_FEDERATION``). Bump ``version``
        inside the secret value to rotate the AgentCore-side credentials.
      * ``app_id`` + ``private_key_base64`` are read directly by the
        repo_helper Lambda via the same secret ARN — no second copy.

    Set to ``null`` to skip the GitHub integration entirely.
  EOT
  type        = string
  default     = null
  nullable    = true
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
