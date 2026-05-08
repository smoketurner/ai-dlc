locals {
  aws_partition  = data.aws_partition.current.partition
  aws_account_id = data.aws_caller_identity.current.account_id

  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"

  common_aws_env = {
    AWS_DEFAULTS_MODE = "in-region"
    AWS_ACCOUNT_ID    = local.aws_account_id
  }
}
