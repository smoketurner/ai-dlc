################################################################################
# Dev environment composition.
#
# The provider's `default_tags` already propagates Project/Env/ManagedBy +
# var.tags down to every taggable resource created by the modules below.
# Each module also stamps a per-resource Name and Component tag of its own.
#
# Module wiring follows the dependency graph:
#   crypto       → no deps
#   network      → no deps
#   registry     → no deps
#   ci_cd        → no deps
#   auth         → no deps
#   state        → crypto (s3-artifacts, dynamodb)
#   messaging    → crypto (secrets)
#   observability → crypto (logs)
#   agents       → crypto, state, auth
################################################################################

module "crypto" {
  source = "../../modules/crypto"

  env = var.env
}

module "network" {
  source = "../../modules/network"

  env               = var.env
  high_availability = false
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

  env = var.env
  # Callback / logout URLs are the dashboard ALB URLs once Phase 7 lands.
  # Until then, leave empty — the user pool stands up without an app client
  # callback configured for production traffic; smoke tests use the hosted UI.
  callback_urls = var.dashboard_callback_urls
  logout_urls   = var.dashboard_logout_urls
}

module "state" {
  source = "../../modules/state"

  env             = var.env
  s3_kms_key_arn  = module.crypto.key_arns["s3-artifacts"]
  ddb_kms_key_arn = module.crypto.key_arns["dynamodb"]
}

module "messaging" {
  source = "../../modules/messaging"

  env         = var.env
  kms_key_arn = module.crypto.key_arns["secrets"]
}

module "observability" {
  source = "../../modules/observability"

  env                         = var.env
  kms_key_arn                 = module.crypto.key_arns["logs"]
  alert_emails                = var.alert_emails
  daily_token_spend_alarm_usd = var.daily_token_spend_alarm_usd
}

module "agents" {
  source = "../../modules/agents"

  env                    = var.env
  memory_kms_key_arn     = module.crypto.key_arns["memory"]
  tokenvault_kms_key_arn = module.crypto.key_arns["tokenvault"]
  logs_kms_key_arn       = module.crypto.key_arns["logs"]
  s3_kms_key_arn         = module.crypto.key_arns["s3-artifacts"]

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
      image_tag        = var.architect_image_tag
    }
    implementer = {
      description      = "Implementer agent — works the tasks list one PR at a time."
      targets          = ["artifact_tool", "repo_helper"]
      bedrock_model_id = "us.anthropic.claude-sonnet-4-6-20260301-v1:0"
      image_tag        = var.implementer_image_tag
    }
  }

  github_oauth = var.github_oauth
}
