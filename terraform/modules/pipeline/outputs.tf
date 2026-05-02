output "state_machine_arn" {
  description = "Step Functions state machine ARN for the SDLC pipeline."
  value       = aws_sfn_state_machine.sdlc.arn
}

output "state_machine_name" {
  description = "Step Functions state machine name."
  value       = aws_sfn_state_machine.sdlc.name
}

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
    hitl_handler    = module.hitl_handler.lambda_function_arn
    event_projector = module.event_projector.lambda_function_arn
  }
}
