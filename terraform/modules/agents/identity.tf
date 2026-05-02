################################################################################
# Identity + token vault.
#
#  * One workload identity per agent (name → ARN map exposed via outputs).
#  * Token vault CMK rotation pinned to our customer-managed key.
#  * Optional GitHub OAuth2 credential provider — gated by var.github_oauth.
#
# Workload identity ARNs feed into per-agent runtime roles (Phase 4) and are
# how the gateway scopes OAuth2 token issuance to the calling agent.
################################################################################

resource "aws_bedrockagentcore_workload_identity" "agent" {
  for_each = var.agents

  name                                = "${local.prefix}-${each.key}"
  allowed_resource_oauth2_return_urls = each.value.allowed_resource_oauth2_return_urls
}

resource "aws_bedrockagentcore_token_vault_cmk" "this" {
  kms_configuration {
    key_type    = "CustomerManagedKey"
    kms_key_arn = var.tokenvault_kms_key_arn
  }
}

resource "aws_bedrockagentcore_oauth2_credential_provider" "github" {
  count = var.github_oauth == null ? 0 : 1

  name                       = "${local.prefix}-github"
  credential_provider_vendor = "GithubOauth2"

  oauth2_provider_config {
    github_oauth2_provider_config {
      client_id_wo                  = var.github_oauth.client_id
      client_secret_wo              = var.github_oauth.client_secret
      client_credentials_wo_version = var.github_oauth.version
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-github"
    Component = "agents"
  })
}
