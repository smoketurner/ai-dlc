locals {
  prefix     = "${var.project}-${var.env}"
  source_dir = "${path.module}/../../../lambdas"

  # Resolve runtime ARNs at template-rendering time. Empty strings when an
  # agent image hasn't been pushed yet — Step Functions will fail fast on
  # invocation, surfacing the missing image rather than running with a stub.
  architect_runtime_arn   = lookup(var.agent_runtime_arns, "architect", "")
  critic_runtime_arn      = lookup(var.agent_runtime_arns, "critic", "")
  implementer_runtime_arn = lookup(var.agent_runtime_arns, "implementer", "")
  reviewer_runtime_arn    = lookup(var.agent_runtime_arns, "reviewer", "")
  tester_runtime_arn      = lookup(var.agent_runtime_arns, "tester", "")

  # Compact list used by the IAM policy doc; only includes ARNs that
  # actually exist (non-empty).
  runtime_arns = compact([
    local.architect_runtime_arn,
    local.critic_runtime_arn,
    local.implementer_runtime_arn,
    local.reviewer_runtime_arn,
    local.tester_runtime_arn,
  ])

  # State-router runtimes — same agents plus triage. Distinct from
  # ``runtime_arns`` because the router invokes triage too (the existing
  # SDLC SFN does not).
  state_router_runtime_arns = compact([
    local.architect_runtime_arn,
    local.critic_runtime_arn,
    local.implementer_runtime_arn,
    local.reviewer_runtime_arn,
    local.tester_runtime_arn,
    var.triage_runtime_arn,
  ])
}
