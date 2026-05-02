locals {
  account_id  = data.aws_caller_identity.current.account_id
  bucket_name = "${var.project}-tfstate-${local.account_id}-${var.region}"
}
