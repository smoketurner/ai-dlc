################################################################################
# Cognito user pool, used by the dashboard ALB listener (OIDC) and the
# AgentCore Gateway / Runtime authorizers (JWT discovery URL).
################################################################################

resource "random_string" "domain_suffix" {
  length  = 8
  upper   = false
  special = false
}

resource "aws_cognito_user_pool" "this" {
  name              = local.pool_name
  mfa_configuration = var.mfa_configuration

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"

  tags = merge(var.tags, {
    Name      = local.pool_name
    Component = "auth"
  })
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = local.domain_name
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_resource_server" "this" {
  identifier   = "https://${var.project}.${var.env}"
  name         = "${local.pool_name}-api"
  user_pool_id = aws_cognito_user_pool.this.id

  dynamic "scope" {
    for_each = local.scope_names
    content {
      scope_name        = scope.value
      scope_description = scope.value
    }
  }
}

resource "aws_cognito_user_pool_client" "this" {
  name                                 = "${local.pool_name}-client"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes = concat(
    ["openid", "email", "profile"],
    [for s in local.scope_names : "${aws_cognito_resource_server.this.identifier}/${s}"],
  )
  callback_urls                = var.callback_urls
  logout_urls                  = var.logout_urls
  supported_identity_providers = ["COGNITO"]

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30
}
