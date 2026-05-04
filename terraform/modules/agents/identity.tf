################################################################################
# Identity.
#
#  * One workload identity per agent (name → ARN map exposed via outputs).
#  * One workload identity for the repo_helper Lambda — used when the Lambda
#    calls AgentCore Identity to fetch user-OBO GitHub tokens.
#  * Token vault uses the AWS service-managed key (default).
#  * GitHub App auth — the ``GithubOauth2`` credential provider reads the
#    App's ``client_id`` + ``client_secret`` from an operator-managed
#    Secrets Manager secret. The same secret carries ``app_id`` +
#    ``private_key_base64`` which the repo_helper Lambda reads directly
#    when minting installation tokens.
#
# Workload identity ARNs feed into per-agent runtime roles (Phase 4) and are
# how AgentCore scopes OAuth2 token issuance to the calling workload.
################################################################################

data "aws_secretsmanager_secret" "github_app" {
  count = var.github_app_secret_name == null ? 0 : 1

  name = var.github_app_secret_name
}

data "aws_secretsmanager_secret_version" "github_app" {
  count = var.github_app_secret_name == null ? 0 : 1

  secret_id = data.aws_secretsmanager_secret.github_app[0].id
}

locals {
  github_app = var.github_app_secret_name == null ? null : jsondecode(data.aws_secretsmanager_secret_version.github_app[0].secret_string)
}

resource "aws_bedrockagentcore_workload_identity" "agent" {
  for_each = var.agents

  name                                = "${local.prefix}-${each.key}"
  allowed_resource_oauth2_return_urls = each.value.allowed_resource_oauth2_return_urls

  # The AgentCore API never returns ``allowed_resource_oauth2_return_urls`` in
  # the read response, so each plan re-shows ``+ allowed_resource_oauth2_return_urls = []``
  # even though the desired state is already in place. Ignore the read-side
  # diff; create-time still sends the configured value.
  lifecycle {
    ignore_changes = [allowed_resource_oauth2_return_urls]
  }
}

resource "aws_bedrockagentcore_workload_identity" "repo_helper" {
  count = var.github_app_secret_name == null ? 0 : 1

  name = "${local.prefix}-repo-helper"
}

# Dashboard workload identity — used by /auth/github to bridge the
# Cognito-authenticated user into AgentCore's USER_FEDERATION on the
# GithubOauth2 credential provider.
resource "aws_bedrockagentcore_workload_identity" "dashboard" {
  count = var.github_app_secret_name == null ? 0 : 1

  name = "${local.prefix}-dashboard"
}

resource "aws_bedrockagentcore_oauth2_credential_provider" "github" {
  count = var.github_app_secret_name == null ? 0 : 1

  name                       = "${local.prefix}-github"
  credential_provider_vendor = "GithubOauth2"

  oauth2_provider_config {
    github_oauth2_provider_config {
      client_id_wo                  = local.github_app.client_id
      client_secret_wo              = local.github_app.client_secret
      client_credentials_wo_version = local.github_app.version
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-github"
    Component = "agents"
  })
}
