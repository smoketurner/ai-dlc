locals {
  aws_account_id = data.aws_caller_identity.current.account_id
  aws_region     = data.aws_region.current.region

  artifacts_name = "${var.project}-${var.env}-artifacts-${local.aws_account_id}-${local.aws_region}"
  memory_md_name = "${var.project}-${var.env}-memory-md-${local.aws_account_id}-${local.aws_region}"
  table_prefix   = "${var.project}-${var.env}"
}
