################################################################################
# HTTP API Gateway. All routes proxy to the dashboard Lambda; auth lives in
# the FastAPI app (SessionMiddleware + Authlib), not at the gateway level —
# both Cognito-authenticated pages and the unauthenticated /webhooks/github
# route are served by the same Lambda.
################################################################################

resource "aws_apigatewayv2_api" "this" {
  name          = local.api_name
  protocol_type = "HTTP"
  description   = "ai-dlc dashboard HTTP API."

  tags = merge(var.tags, {
    Name      = local.api_name
    Component = "dashboard"
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = local.log_group_api
  retention_in_days = var.log_retention_days

  tags = merge(var.tags, {
    Name      = local.log_group_api
    Component = "dashboard"
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
    })
  }

  tags = merge(var.tags, {
    Name      = "${local.api_name}-default"
    Component = "dashboard"
  })
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.function.lambda_function_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = var.lambda_timeout_seconds * 1000
}

# A single proxy route catches everything Mangum forwards to FastAPI;
# FastAPI dispatches by method + path inside the Lambda.
resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "root" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "ANY /"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.function.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

resource "aws_apigatewayv2_domain_name" "this" {
  count = local.use_https ? 1 : 0

  domain_name = var.dashboard_fqdn

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.this[0].certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = merge(var.tags, {
    Name      = var.dashboard_fqdn
    Component = "dashboard"
  })
}

resource "aws_apigatewayv2_api_mapping" "this" {
  count = local.use_https ? 1 : 0

  api_id      = aws_apigatewayv2_api.this.id
  domain_name = aws_apigatewayv2_domain_name.this[0].domain_name
  stage       = aws_apigatewayv2_stage.default.id
}
