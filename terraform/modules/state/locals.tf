locals {
  account_id     = data.aws_caller_identity.current.account_id
  region         = data.aws_region.current.region
  artifacts_name = "${var.project}-${var.env}-artifacts-${local.account_id}-${local.region}"
  memory_md_name = "${var.project}-${var.env}-memory-md-${local.account_id}-${local.region}"
  table_prefix   = "${var.project}-${var.env}"
}
