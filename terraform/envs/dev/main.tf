################################################################################
# Dev environment composition.
#
# The provider's `default_tags` already propagates Project/Env/ManagedBy +
# var.tags down to every taggable resource created by the modules below.
# Each module also stamps a per-resource Name and Component tag of its own.
#
# Module wiring follows the dependency graph:
#   network      → no deps
#   registry     → no deps
#   ci_cd        → no deps
#   auth         → no deps
#   state        → no deps
#   messaging    → no deps
#   observability → no deps
#   agents       → state, auth
#   pipeline     → state, messaging, auth, agents
#   dashboard    → network, registry, state, messaging, auth, pipeline
#   improvement  → state, messaging
################################################################################

data "aws_route53_zone" "bootstrap" {
  name         = var.dns_zone_name
  private_zone = false
}

locals {
  dashboard_fqdn = "dashboard-${var.env}.${var.dns_zone_name}"
  dashboard_url  = "https://${local.dashboard_fqdn}"

  dashboard_callback_urls = coalesce(var.dashboard_callback_urls, ["${local.dashboard_url}/auth/callback"])
  dashboard_logout_urls   = coalesce(var.dashboard_logout_urls, [local.dashboard_url])

  # Agents whose ECR image has been pushed at least once. AgentCore Runtimes
  # are only created for these — agents missing an image get IAM / gateway /
  # identity but no runtime, so `terraform apply` works before the first
  # `images-build` run. Held in a local so the improvement module can read
  # ``contains(keys(...), "proposer")`` at plan time (the runtime ARN itself
  # may be unknown when first being created, which would break ``count``).
  # New agents land here only after their first image is pushed via the
  # images-build workflow — the runtime resource's ECR data source fails
  # if no image exists. Procedure: register the agent in var.agents,
  # apply (creates ECR repo + IAM + gateway), trigger images-build, then
  # add the agent here and re-apply (creates the runtime).
  agent_image_tags = {
    architect    = "latest"
    code_critic  = "latest"
    critic       = "latest"
    implementer  = "latest"
    proposer     = "latest"
    retrospector = "latest"
    reviewer     = "latest"
    tester       = "latest"
    triage       = "latest"
  }
}

module "registry" {
  source = "../../modules/registry"
}

module "ci_cd" {
  source = "../../modules/ci_cd"

  github_owner = var.github_owner
  github_repo  = var.github_repo
}

module "auth" {
  source = "../../modules/auth"

  env           = var.env
  callback_urls = local.dashboard_callback_urls
  logout_urls   = local.dashboard_logout_urls
}

module "state" {
  source = "../../modules/state"

  env = var.env
}

module "messaging" {
  source = "../../modules/messaging"

  env = var.env
}

module "observability" {
  source = "../../modules/observability"

  env                         = var.env
  alert_emails                = var.alert_emails
  daily_token_spend_alarm_usd = var.daily_token_spend_alarm_usd
}

module "agents" {
  source = "../../modules/agents"

  env = var.env

  bus_name = module.messaging.bus_name
  bus_arn  = module.messaging.bus_arn

  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn
  memory_md_bucket     = module.state.memory_md_bucket
  memory_md_bucket_arn = module.state.memory_md_bucket_arn

  cognito_discovery_url = module.auth.discovery_url
  cognito_audience      = [module.auth.client_id]

  ecr_repository_urls = module.registry.repository_urls

  agents = {
    architect = {
      description = "Architect agent — writes the spec bundle (requirements + design + tasks)."
      targets     = ["artifact_tool"]
      features    = ["browser"]
      # Temporarily on Sonnet 4.6 while the Bedrock daily-token quota
      # increase for Opus 4.6 V1 is pending support review. Revert when
      # the quota lands.
      # bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    critic = {
      description = "Critic agent — adversarially reviews the spec (advisory)."
      targets     = ["artifact_tool"]
      features    = ["browser"]
      # bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    code_critic = {
      description = "Code-Critic agent — adversarially reviews the integrated impl PR (advisory)."
      targets     = ["artifact_tool", "repo_helper"]
      features    = ["browser", "code_interpreter"]
      # bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    implementer = {
      description      = "Implementer agent — works the tasks list one PR at a time."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    reviewer = {
      description      = "Reviewer agent — code-reviews each task PR (advisory)."
      targets          = ["artifact_tool", "repo_helper"]
      features         = ["browser", "code_interpreter"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    tester = {
      description      = "Tester agent — flags test gaps in each task PR (advisory)."
      targets          = ["artifact_tool", "repo_helper"]
      features         = ["browser", "code_interpreter"]
      bedrock_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
    proposer = {
      description = "Proposer agent — research-driven; opens PRs proposing prompt/MEMORY edits."
      targets     = ["repo_helper"]
      features    = ["browser"]
      # bedrock_model_id = "us.anthropic.claude-opus-4-6-v1"
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6"
    }
    retrospector = {
      description      = "Retrospector agent — fires on every terminal event; appends lessons to MEMORY.md via PR."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
    triage = {
      description      = "Triage agent — classifies tagged GitHub issues and routes them into a workflow phase."
      targets          = []
      bedrock_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
  }

  agent_image_tags = local.agent_image_tags

  github_app_secret_name     = var.github_app_secret_name
  dashboard_oauth_return_url = "${local.dashboard_url}/auth/github/callback"

  common_layer_arn = module.common_layer.lambda_layer_arn
}

resource "aws_secretsmanager_secret" "github_webhook" {
  name                    = "${var.project}-${var.env}/github-webhook-secret"
  description             = "HMAC signing secret for the GitHub webhook receiver."
  recovery_window_in_days = 7

  tags = {
    Name      = "${var.project}-${var.env}-github-webhook-secret"
    Component = "dashboard"
  }
}

module "pipeline" {
  source = "../../modules/pipeline"

  env = var.env

  bus_name = module.messaging.bus_name
  bus_arn  = module.messaging.bus_arn

  runs_table            = module.state.runs_table
  runs_table_arn        = module.state.runs_table_arn
  runs_stream_arn       = module.state.runs_stream_arn
  idempotency_table     = module.state.idempotency_table
  idempotency_table_arn = module.state.idempotency_table_arn

  memory_id  = module.agents.memory_id
  memory_arn = module.agents.memory_arn

  agent_runtime_arns = module.agents.runtime_arns

  repo_helper_function_name = element(split(":", module.agents.tool_lambda_arns["repo_helper"]), 6)
  repo_helper_function_arn  = module.agents.tool_lambda_arns["repo_helper"]

  triage_runtime_arn   = lookup(module.agents.runtime_arns, "triage", "")
  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn

  cognito_user_pool_arn = module.auth.user_pool_arn
  cognito_audience      = [module.auth.client_id]
  cognito_issuer_url    = module.auth.issuer_url

  common_layer_arn = module.common_layer.lambda_layer_arn

  beacon_queue_url = module.messaging.state_router_queue_url
  beacon_queue_arn = module.messaging.state_router_queue_arn
}

module "dashboard" {
  source = "../../modules/dashboard"

  env              = var.env
  common_layer_arn = module.common_layer.lambda_layer_arn

  dashboard_fqdn  = local.dashboard_fqdn
  route53_zone_id = data.aws_route53_zone.bootstrap.zone_id

  bus_name = module.messaging.bus_name
  bus_arn  = module.messaging.bus_arn

  runs_table            = module.state.runs_table
  runs_table_arn        = module.state.runs_table_arn
  idempotency_table     = module.state.idempotency_table
  idempotency_table_arn = module.state.idempotency_table_arn

  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn

  beacon_queue_url = module.messaging.state_router_queue_url
  beacon_queue_arn = module.messaging.state_router_queue_arn

  github_webhook_secret_id  = aws_secretsmanager_secret.github_webhook.name
  github_webhook_secret_arn = aws_secretsmanager_secret.github_webhook.arn
  github_app_secret_arn     = module.agents.github_app_secret_arn

  cognito_user_pool_id        = module.auth.user_pool_id
  cognito_user_pool_client_id = module.auth.client_id
  cognito_user_pool_domain    = module.auth.domain
  cognito_client_secret_id    = module.auth.client_secret_id
  cognito_client_secret_arn   = module.auth.client_secret_arn
  cognito_discovery_url       = module.auth.discovery_url

  dashboard_workload_name    = module.agents.dashboard_workload_name
  github_oauth_provider_name = module.agents.github_oauth_provider_name
  dashboard_oauth_return_url = module.agents.dashboard_oauth_return_url
  github_bot_login           = var.github_bot_login
}

module "improvement" {
  source = "../../modules/improvement"

  env = var.env

  bus_name = module.messaging.bus_name
  bus_arn  = module.messaging.bus_arn

  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn

  retrospector_runtime_arn = lookup(module.agents.runtime_arns, "retrospector", "")
  retrospector_enabled     = contains(keys(local.agent_image_tags), "retrospector")

  common_layer_arn = module.common_layer.lambda_layer_arn
}
