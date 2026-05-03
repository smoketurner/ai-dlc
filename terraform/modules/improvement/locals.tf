locals {
  aws_partition  = data.aws_partition.current.partition
  aws_account_id = data.aws_caller_identity.current.account_id

  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"
}
