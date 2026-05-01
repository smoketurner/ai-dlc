locals {
  prefix          = "${var.project}-${var.env}"
  app_log_group   = "/${var.project}/${var.env}/app"
  alerts_topic    = "${local.prefix}-alerts"
  dashboard_name  = "${local.prefix}-overview"
  agent_namespace = "ai-dlc/agents"
}
