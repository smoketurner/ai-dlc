output "url" {
  description = "Public URL for the dashboard."
  value       = local.use_https ? "https://${var.dashboard_fqdn}" : aws_apigatewayv2_api.this.api_endpoint
}

output "api_endpoint" {
  description = "Raw API Gateway execution URL (always available, regardless of custom domain)."
  value       = aws_apigatewayv2_api.this.api_endpoint
}

output "lambda_function_name" {
  description = "Name of the dashboard Lambda."
  value       = module.function.lambda_function_name
}

output "lambda_function_arn" {
  description = "ARN of the dashboard Lambda."
  value       = module.function.lambda_function_arn
}

output "lambda_role_arn" {
  description = "ARN of the dashboard Lambda's execution role."
  value       = module.function.lambda_role_arn
}
