################################################################################
# Identity.
#
#  * One workload identity per agent (name → ARN map exposed via outputs).
#  * One workload identity for the repo_helper Lambda — used when the Lambda
#    calls AgentCore Identity to fetch user-OBO GitHub tokens.
#  * Token vault uses the AWS service-managed key (default).
#  * GitHub App auth — the ``GithubOauth2`` credential provider handles the
#    user-on-behalf-of (USER_FEDERATION) flow; the App's private key for
#    installation-token minting goes into a separate Secrets Manager secret
#    that the repo_helper Lambda reads at runtime.
#
# Workload identity ARNs feed into per-agent runtime roles (Phase 4) and are
# how AgentCore scopes OAuth2 token issuance to the calling workload.
################################################################################

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
  count = var.github_app == null ? 0 : 1

  name = "${local.prefix}-repo-helper"
}

# Dashboard workload identity — used by /auth/github to bridge the
# Cognito-authenticated user into AgentCore's USER_FEDERATION on the
# GithubOauth2 credential provider.
resource "aws_bedrockagentcore_workload_identity" "dashboard" {
  count = var.github_app == null ? 0 : 1

  name = "${local.prefix}-dashboard"
}

resource "aws_bedrockagentcore_oauth2_credential_provider" "github" {
  count = var.github_app == null ? 0 : 1

  name                       = "${local.prefix}-github"
  credential_provider_vendor = "GithubOauth2"

  oauth2_provider_config {
    github_oauth2_provider_config {
      client_id_wo                  = var.github_app.client_id
      client_secret_wo              = var.github_app.client_secret
      client_credentials_wo_version = var.github_app.version
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-github"
    Component = "agents"
  })
}

# Secret holding the GitHub App's private key + numeric app_id, used by the
# repo_helper Lambda when it mints installation-scoped access tokens (the
# bot-attribution fallback path). User-OBO tokens flow through AgentCore
# Identity above; this secret is only for the install path.
resource "aws_secretsmanager_secret" "github_app" {
  count = var.github_app == null ? 0 : 1

  name                    = "${local.prefix}/github-app"
  description             = "GitHub App app_id + private_key for installation-token minting."
  recovery_window_in_days = 7

  tags = merge(var.tags, {
    Name      = "${local.prefix}-github-app"
    Component = "agents"
  })
}

resource "aws_secretsmanager_secret_version" "github_app" {
  count = var.github_app == null ? 0 : 1

  secret_id = aws_secretsmanager_secret.github_app[0].id
  secret_string = jsonencode({
    app_id      = var.github_app.app_id
    private_key = var.github_app.private_key
  })
}
