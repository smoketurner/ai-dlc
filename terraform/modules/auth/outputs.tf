output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.this.arn
}

output "client_id" {
  value = aws_cognito_user_pool_client.this.id
}

output "client_secret" {
  value     = aws_cognito_user_pool_client.this.client_secret
  sensitive = true
}

output "client_secret_id" {
  description = "Secrets Manager secret id holding the Cognito app-client secret."
  value       = aws_secretsmanager_secret.cognito_client.id
}

output "client_secret_arn" {
  description = "Secrets Manager secret ARN holding the Cognito app-client secret."
  value       = aws_secretsmanager_secret.cognito_client.arn
}

output "domain" {
  value = aws_cognito_user_pool_domain.this.domain
}

output "issuer_url" {
  value = "https://cognito-idp.${local.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
}

output "discovery_url" {
  value = "https://cognito-idp.${local.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
}
