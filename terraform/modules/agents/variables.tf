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

    `bedrock_fallback_model_id`, when set, is plumbed into the agent
    container as `AIDLC_BEDROCK_FALLBACK_MODEL_ID`. The agent's
    `common.runtime.invoke_with_fallback` catches a
    `ModelThrottledException` from the primary model (after the retry
    strategy is exhausted) and re-runs the workflow once on the fallback.
    Useful for keeping Opus as the primary while letting daily-token-quota
    throttles transparently degrade to Sonnet.
  EOT
  type = map(object({
    description                         = string
    targets                             = set(string)
    features                            = optional(set(string), [])
    allowed_resource_oauth2_return_urls = optional(list(string), [])
    bedrock_model_id                    = optional(string, "")
    bedrock_fallback_model_id           = optional(string, "")
  }))
  default = {
    architect = {
      description               = "Architect agent — writes the spec bundle (requirements + design + tasks)."
      targets                   = ["artifact_tool"]
      bedrock_model_id          = "us.anthropic.claude-opus-4-6-v1"
      bedrock_fallback_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    code_critic = {
      description               = "Code-Critic agent — adversarially reviews the integrated impl PR (advisory)."
      targets                   = ["artifact_tool", "repo_helper"]
      features                  = ["code_interpreter"]
      bedrock_model_id          = "us.anthropic.claude-opus-4-6-v1"
      bedrock_fallback_model_id = "us.anthropic.claude-sonnet-4-6"
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
      description               = "Proposer agent — research-driven; opens PRs proposing prompt/MEMORY edits."
      targets                   = ["repo_helper"]
      features                  = ["browser"]
      bedrock_model_id          = "us.anthropic.claude-opus-4-6-v1"
      bedrock_fallback_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    retrospector = {
      description      = "Retrospector agent — fires on every terminal event; appends lessons to MEMORY.md via PR."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
    triage = {
      description      = "Triage agent — classifies tagged GitHub issues and routes them into a workflow phase."
      targets          = []
      features         = ["browser"]
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
  description = <<-EOT
    Cognito OpenID Connect discovery URL. The per-agent AgentCore Gateway
    authorizer validates Cognito-issued JWTs against this URL. The agent
    runtime obtains those JWTs from AgentCore Identity via the M2M
    (client_credentials) credential provider configured below.
  EOT
  type        = string
}

variable "cognito_gateway_m2m_client_id" {
  description = <<-EOT
    Cognito M2M (client_credentials) app client id. The AgentCore Gateway
    authorizer accepts JWTs whose ``client_id`` claim matches this value;
    AgentCore Identity mints those JWTs through the OAuth2 credential
    provider configured below.
  EOT
  type        = string
}

variable "cognito_gateway_m2m_client_secret" {
  description = "Cognito M2M app client secret. Stored in AgentCore Identity's token vault."
  type        = string
  sensitive   = true
}

variable "cognito_gateway_m2m_scope" {
  description = "OAuth2 scope the agent requests when fetching its Cognito M2M token (full ``<resource>/<scope>`` string)."
  type        = string
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

variable "common_layer_arn" {
  description = "ARN of the shared Lambda layer carrying the `common` Python package."
  type        = string
}

variable "bus_name" {
  description = <<-EOT
    EventBridge platform bus name. Threaded into every agent runtime as
    ``AIDLC_BUS_NAME`` so the agent can emit its completion event
    (``DESIGN.READY``, ``IMPL_PR.OPENED``, ``REVIEW.READY``,
    ``TEST_REPORT.READY``, ``CODE_CRITIQUE.READY``, ``REVISION.READY``,
    ``ISSUE.TRIAGED``).
  EOT
  type        = string
}

variable "bus_arn" {
  description = "ARN of the EventBridge platform bus (target of events:PutEvents IAM grant)."
  type        = string
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
