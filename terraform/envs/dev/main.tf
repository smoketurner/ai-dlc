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

  dashboard_callback_urls = coalesce(var.dashboard_callback_urls, ["${local.dashboard_url}/oauth2/idpresponse"])
  dashboard_logout_urls   = coalesce(var.dashboard_logout_urls, ["${local.dashboard_url}/logout"])
}

module "network" {
  source = "../../modules/network"

  env = var.env
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

  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn
  memory_md_bucket     = module.state.memory_md_bucket
  memory_md_bucket_arn = module.state.memory_md_bucket_arn

  cognito_discovery_url = module.auth.discovery_url
  cognito_audience      = [module.auth.client_id]

  ecr_repository_urls = module.registry.repository_urls

  agents = {
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

  github_app = var.github_app
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
  approvals_table       = module.state.approvals_table
  approvals_table_arn   = module.state.approvals_table_arn
  approvals_stream_arn  = module.state.approvals_stream_arn
  idempotency_table     = module.state.idempotency_table
  idempotency_table_arn = module.state.idempotency_table_arn

  memory_id  = module.agents.memory_id
  memory_arn = module.agents.memory_arn

  agent_runtime_arns = module.agents.runtime_arns

  cognito_user_pool_arn = module.auth.user_pool_arn
  cognito_audience      = [module.auth.client_id]
  cognito_issuer_url    = module.auth.issuer_url
}

module "dashboard" {
  source = "../../modules/dashboard"

  env                = var.env
  ecr_repository_url = module.registry.repository_urls["dashboard"]

  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  private_subnet_ids = module.network.private_subnet_ids

  alb_log_bucket  = module.state.artifacts_bucket
  dashboard_fqdn  = local.dashboard_fqdn
  route53_zone_id = data.aws_route53_zone.bootstrap.zone_id

  bus_name = module.messaging.bus_name
  bus_arn  = module.messaging.bus_arn

  runs_table            = module.state.runs_table
  runs_table_arn        = module.state.runs_table_arn
  approvals_table       = module.state.approvals_table
  approvals_table_arn   = module.state.approvals_table_arn
  idempotency_table     = module.state.idempotency_table
  idempotency_table_arn = module.state.idempotency_table_arn

  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn

  hitl_handler_function_name = "${var.project}-${var.env}-hitl-handler"
  hitl_handler_function_arn  = module.pipeline.lambda_arns["hitl_handler"]

  github_webhook_secret_id  = aws_secretsmanager_secret.github_webhook.name
  github_webhook_secret_arn = aws_secretsmanager_secret.github_webhook.arn

  cognito_user_pool_arn       = module.auth.user_pool_arn
  cognito_user_pool_id        = module.auth.user_pool_id
  cognito_user_pool_client_id = module.auth.client_id
  cognito_user_pool_domain    = module.auth.domain
}

module "improvement" {
  source = "../../modules/improvement"

  env = var.env

  bus_name = module.messaging.bus_name
  bus_arn  = module.messaging.bus_arn

  runs_table      = module.state.runs_table
  runs_table_arn  = module.state.runs_table_arn
  runs_stream_arn = module.state.runs_stream_arn

  artifacts_bucket     = module.state.artifacts_bucket
  artifacts_bucket_arn = module.state.artifacts_bucket_arn

  sdlc_state_machine_arn = module.pipeline.state_machine_arn
  alerts_topic_arn       = module.observability.alerts_topic_arn
}
