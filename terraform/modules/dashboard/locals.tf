locals {
  aws_partition  = data.aws_partition.current.partition
  aws_account_id = data.aws_caller_identity.current.account_id
  aws_region     = data.aws_region.current.region

  prefix         = "${var.project}-${var.env}"
  cluster_name   = "${local.prefix}-dashboard"
  service_name   = "${local.prefix}-dashboard"
  alb_name       = "${local.prefix}-dashboard"
  task_family    = "${local.prefix}-dashboard"
  log_group_name = "/aws/ecs/${local.service_name}"
  alb_log_prefix = "alb/${local.prefix}-dashboard"
  use_https      = var.dashboard_fqdn != null && var.route53_zone_id != null
  webhook_path   = "/webhooks/github"

  common_aws_env = {
    AWS_DEFAULTS_MODE = "in-region"
    AWS_ACCOUNT_ID    = local.aws_account_id
  }
}
