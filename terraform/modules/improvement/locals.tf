locals {
  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"
}
