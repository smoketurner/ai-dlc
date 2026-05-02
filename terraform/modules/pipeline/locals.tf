locals {
  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"

  # Resolve runtime ARNs at template-rendering time. Empty strings when an
  # agent image hasn't been pushed yet — Step Functions will fail fast on
  # invocation, surfacing the missing image rather than running with a stub.
  architect_runtime_arn   = lookup(var.agent_runtime_arns, "architect", "")
  implementer_runtime_arn = lookup(var.agent_runtime_arns, "implementer", "")

  # Compact list used by the IAM policy doc; only includes ARNs that
  # actually exist (non-empty).
  runtime_arns = compact([local.architect_runtime_arn, local.implementer_runtime_arn])
}
