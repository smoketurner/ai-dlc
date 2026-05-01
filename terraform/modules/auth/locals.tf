locals {
  pool_name   = "${var.project}-${var.env}"
  domain_name = "${var.project}-${var.env}-${random_string.domain_suffix.result}"
  scope_names = ["runs:write", "runs:read", "approvals:write"]
}
