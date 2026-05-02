locals {
  prefix         = "${var.project}-${var.env}"
  cluster_name   = "${local.prefix}-dashboard"
  service_name   = "${local.prefix}-dashboard"
  alb_name       = "${local.prefix}-dashboard"
  task_family    = "${local.prefix}-dashboard"
  log_group_name = "/aws/ecs/${local.service_name}"
  alb_log_prefix = "alb/${local.prefix}-dashboard"
  has_image      = var.image_tag != ""
  use_https      = var.alb_acm_certificate_arn != null
  webhook_path   = "/webhooks/github"
}
