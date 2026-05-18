################################################################################
# Identity.
#
#  * One SHARED workload identity for every platform component that calls
#    AgentCore Identity APIs on behalf of the user — dashboard, every
#    agent runtime, and the repo_helper Lambda. The Token Vault is keyed
#    on (workload, user, credential_provider); the dashboard's
#    /auth/github flow saves the user's GitHub OAuth token under
#    (this workload, user.sub, github), and every agent reads it back
#    using the same triple. Per-agent workloads previously fragmented
#    the vault — token saved by dashboard, lookup keyed by implementer →
#    silent miss → fallback to App-installation-token attribution.
#  * Token vault uses the AWS service-managed key (default).
#  * GitHub App auth — the ``GithubOauth2`` credential provider reads the
#    App's ``client_id`` + ``client_secret`` from an operator-managed
#    Secrets Manager secret. The same secret carries ``app_id`` +
#    ``private_key_base64`` which the repo_helper Lambda reads directly
#    when minting installation tokens.
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

# Single platform-wide workload identity. Used by the dashboard's
# /auth/github flow to register the user's GitHub OAuth token, and by
# every agent runtime + the repo_helper Lambda to fetch it back via OBO.
# Created only when the GitHub App is configured — without it there's no
# OAuth provider to federate against, so no need for a workload identity.
resource "aws_bedrockagentcore_workload_identity" "platform" {
  count = var.github_app_secret_name == null ? 0 : 1

  name                                = "${local.prefix}-platform"
  allowed_resource_oauth2_return_urls = var.dashboard_oauth_return_url == "" ? [] : [var.dashboard_oauth_return_url]

  # The AgentCore API never returns ``allowed_resource_oauth2_return_urls``
  # in the read response, so each plan re-shows ``+ allowed_… = []`` even
  # when the desired state is already in place. Ignore the read-side diff;
  # create-time still sends the configured value.
  lifecycle {
    ignore_changes = [allowed_resource_oauth2_return_urls]
  }
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

# Cognito M2M (client_credentials) credential provider. The agent
# runtime fetches a Cognito JWT through this provider via
# ``IdentityClient.get_resource_oauth2_token`` (or the SDK's
# ``@requires_access_token(auth_flow="M2M")`` decorator) and uses it
# as the Bearer header against its AgentCore Gateway.
resource "aws_bedrockagentcore_oauth2_credential_provider" "cognito_gateway_m2m" {
  name                       = "${local.prefix}-cognito-gateway-m2m"
  credential_provider_vendor = "CustomOauth2"

  oauth2_provider_config {
    custom_oauth2_provider_config {
      client_id     = var.cognito_gateway_m2m_client_id
      client_secret = var.cognito_gateway_m2m_client_secret

      oauth_discovery {
        discovery_url = var.cognito_discovery_url
      }
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-cognito-gateway-m2m"
    Component = "agents"
  })
}
