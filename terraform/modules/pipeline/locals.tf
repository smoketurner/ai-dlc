locals {
  aws_account_id = data.aws_caller_identity.current.account_id

  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"

  common_aws_env = {
    AWS_DEFAULTS_MODE = "in-region"
    AWS_ACCOUNT_ID    = local.aws_account_id
  }

  # Resolve runtime ARNs at template-rendering time. Empty strings when
  # an agent image hasn't been pushed yet — the state_router's dispatch
  # handlers Noop when the corresponding env var is empty rather than
  # invoking with a stub ARN.
  architect_runtime_arn   = lookup(var.agent_runtime_arns, "architect", "")
  code_critic_runtime_arn = lookup(var.agent_runtime_arns, "code_critic", "")
  implementer_runtime_arn = lookup(var.agent_runtime_arns, "implementer", "")
  proposer_runtime_arn    = lookup(var.agent_runtime_arns, "proposer", "")
  reviewer_runtime_arn    = lookup(var.agent_runtime_arns, "reviewer", "")
  tester_runtime_arn      = lookup(var.agent_runtime_arns, "tester", "")

  # Compact list of every runtime ARN the state_router can invoke
  # (architect, code_critic, implementer, proposer, reviewer, tester,
  # triage). Used by the IAM policy doc; only includes ARNs that
  # actually exist.
  state_router_runtime_arns = compact([
    local.architect_runtime_arn,
    local.code_critic_runtime_arn,
    local.implementer_runtime_arn,
    local.proposer_runtime_arn,
    local.reviewer_runtime_arn,
    local.tester_runtime_arn,
    var.triage_runtime_arn,
  ])
}
