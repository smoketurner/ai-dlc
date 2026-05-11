################################################################################
# Cognito user-pool app-client secret, mirrored into Secrets Manager so the
# dashboard Lambda can read it at runtime via boto3. The Cognito-managed
# secret itself is not addressable from `secretsmanager:GetSecretValue`, so
# every consumer would otherwise have to ship the value through env vars or
# duplicate it. Storing it once here keeps the secret out of state files
# read by humans and gives the dashboard a stable secret_id to read.
################################################################################

resource "aws_secretsmanager_secret" "cognito_client" {
  name                    = "${var.project}-${var.env}/cognito-client-secret"
  description             = "Cognito user-pool app-client secret consumed by the dashboard."
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Name      = "${var.project}-${var.env}-cognito-client-secret"
    Component = "auth"
  })
}

resource "aws_secretsmanager_secret_version" "cognito_client" {
  secret_id     = aws_secretsmanager_secret.cognito_client.id
  secret_string = aws_cognito_user_pool_client.this.client_secret
}
