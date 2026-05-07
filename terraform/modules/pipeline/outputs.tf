output "api_endpoint" {
  description = "API Gateway invoke URL (default stage)."
  value       = aws_apigatewayv2_api.this.api_endpoint
}

output "api_id" {
  description = "API Gateway API ID."
  value       = aws_apigatewayv2_api.this.id
}

output "lambda_arns" {
  description = "Map of platform Lambda name → ARN."
  value = {
    entry_adapter   = module.entry_adapter.lambda_function_arn
    state_router    = module.state_router.lambda_function_arn
    event_projector = module.event_projector.lambda_function_arn
  }
}

output "state_router_function_name" {
  description = "Name of the state_router Lambda."
  value       = module.state_router.lambda_function_name
}

output "state_router_function_arn" {
  description = "ARN of the state_router Lambda."
  value       = module.state_router.lambda_function_arn
}
