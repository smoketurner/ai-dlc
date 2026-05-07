locals {
  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"

  # Resolve runtime ARNs at template-rendering time. Empty strings when
  # an agent image hasn't been pushed yet — the state_router's dispatch
  # handlers Noop when the corresponding env var is empty rather than
  # invoking with a stub ARN.
  architect_runtime_arn   = lookup(var.agent_runtime_arns, "architect", "")
  critic_runtime_arn      = lookup(var.agent_runtime_arns, "critic", "")
  implementer_runtime_arn = lookup(var.agent_runtime_arns, "implementer", "")
  reviewer_runtime_arn    = lookup(var.agent_runtime_arns, "reviewer", "")
  tester_runtime_arn      = lookup(var.agent_runtime_arns, "tester", "")

  # Compact list of every runtime ARN the state_router can invoke
  # (architect, critic, implementer, reviewer, tester, triage). Used by
  # the IAM policy doc; only includes ARNs that actually exist.
  state_router_runtime_arns = compact([
    local.architect_runtime_arn,
    local.critic_runtime_arn,
    local.implementer_runtime_arn,
    local.reviewer_runtime_arn,
    local.tester_runtime_arn,
    var.triage_runtime_arn,
  ])
}
