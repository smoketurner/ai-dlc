locals {
  account_id     = data.aws_caller_identity.current.account_id
  artifacts_name = "${var.project}-artifacts-${var.env}-${local.account_id}"
  memory_md_name = "${var.project}-memory-md-${var.env}-${local.account_id}"
  table_prefix   = "${var.project}-${var.env}"
}
