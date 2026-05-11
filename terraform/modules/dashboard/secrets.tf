################################################################################
# Session signing key. Starlette's SessionMiddleware uses this to HMAC-sign
# the session cookie, so a rotation invalidates every active session.
# Stored in Secrets Manager so the Lambda role can read it at cold start;
# the random value never appears in plain Terraform state.
################################################################################

resource "random_password" "session_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "session" {
  name                    = "${local.prefix}/dashboard-session-secret"
  description             = "SessionMiddleware signing key for the dashboard Lambda."
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Name      = "${local.prefix}-dashboard-session-secret"
    Component = "dashboard"
  })
}

resource "aws_secretsmanager_secret_version" "session" {
  secret_id     = aws_secretsmanager_secret.session.id
  secret_string = random_password.session_secret.result
}
