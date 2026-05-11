locals {
  aws_partition  = data.aws_partition.current.partition
  aws_account_id = data.aws_caller_identity.current.account_id
  aws_region     = data.aws_region.current.region

  prefix           = "${var.project}-${var.env}"
  function_name    = "${local.prefix}-dashboard"
  api_name         = "${local.prefix}-dashboard-api"
  log_group_lambda = "/aws/lambda/${local.function_name}"
  log_group_api    = "/aws/apigateway/${local.api_name}"
  use_https        = var.dashboard_fqdn != null && var.route53_zone_id != null
  dashboard_url    = local.use_https ? "https://${var.dashboard_fqdn}" : aws_apigatewayv2_api.this.api_endpoint

  common_aws_env = {
    AWS_DEFAULTS_MODE = "in-region"
    AWS_ACCOUNT_ID    = local.aws_account_id
  }
}
