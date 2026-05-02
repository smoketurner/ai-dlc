################################################################################
# HTTP API Gateway. Two routes (both JWT-protected):
#
#   POST /v1/runs                  — entry_adapter Lambda
#   POST /v1/runs/{run_id}/decide  — hitl_handler Lambda (DECIDE)
#
# The GitHub PR webhook is *not* on this API Gateway — it lands on a
# separate, unauthenticated rule on the dashboard ALB at /webhooks/github.
# The dashboard verifies the HMAC signature in-app and forwards to the
# hitl_handler Lambda directly.
################################################################################

resource "aws_apigatewayv2_api" "this" {
  name          = "${local.prefix}-api"
  protocol_type = "HTTP"
  description   = "ai-dlc public API."

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type", "x-aidlc-correlation-id"]
    max_age       = 600
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-api"
    Component = "pipeline"
  })
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.this.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito-jwt"

  jwt_configuration {
    audience = var.cognito_audience
    issuer   = var.cognito_issuer_url
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/${local.prefix}-api"
  retention_in_days = var.lambda_log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(var.tags, {
    Name      = "/aws/apigateway/${local.prefix}-api"
    Component = "pipeline"
  })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    detailed_metrics_enabled = true
    throttling_burst_limit   = 50
    throttling_rate_limit    = 25
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      protocol       = "$context.protocol"
      responseLength = "$context.responseLength"
      jwtSub         = "$context.authorizer.claims.sub"
    })
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-api-default"
    Component = "pipeline"
  })
}

# POST /v1/runs → entry_adapter

resource "aws_apigatewayv2_integration" "entry_adapter" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.entry_adapter.lambda_function_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_runs" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "POST /v1/runs"
  target             = "integrations/${aws_apigatewayv2_integration.entry_adapter.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

resource "aws_lambda_permission" "apigw_invoke_entry_adapter" {
  statement_id  = "AllowAPIGatewayInvokeEntryAdapter"
  action        = "lambda:InvokeFunction"
  function_name = module.entry_adapter.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# POST /v1/runs/{run_id}/decide → hitl_handler (DECIDE op)

resource "aws_apigatewayv2_integration" "hitl_handler" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.hitl_handler.lambda_function_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_decide" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "POST /v1/runs/{run_id}/decide"
  target             = "integrations/${aws_apigatewayv2_integration.hitl_handler.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

resource "aws_lambda_permission" "apigw_invoke_hitl" {
  statement_id  = "AllowAPIGatewayInvokeHitl"
  action        = "lambda:InvokeFunction"
  function_name = module.hitl_handler.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
