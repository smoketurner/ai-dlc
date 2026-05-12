locals {
  aws_region = data.aws_region.current.region

  pool_name   = "${var.project}-${var.env}"
  domain_name = "${var.project}-${var.env}-${random_string.domain_suffix.result}"
  scope_names = ["runs:write", "runs:read"]

  # Scope on the gateway resource server. Agents exchange their workload
  # access token for a Cognito-issued M2M JWT carrying this scope, then
  # use it as the Bearer header against their AgentCore Gateway.
  gateway_scope_name = "invoke"
  gateway_resource   = "https://${var.project}.${var.env}/gateway"
  gateway_full_scope = "${local.gateway_resource}/${local.gateway_scope_name}"
}
