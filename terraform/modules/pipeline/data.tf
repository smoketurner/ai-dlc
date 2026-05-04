data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Trust policy for the Step Functions state machine role.
data "aws_iam_policy_document" "states_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

# Permissions granted to the state machine role.
data "aws_iam_policy_document" "states_inline" {
  # Only emit the InvokeAgentRuntime statement when at least one agent
  # runtime ARN is known. Empty resources are rejected by IAM, and AWS
  # then silently drops the whole inline policy — which surfaces later
  # as "AccessDeniedException ... Log Destination" on state-machine
  # creation. Bootstrap path: first apply with no images creates the
  # state machine without the runtime perm; once images are pushed and
  # var.agent_runtime_arns is populated, re-apply attaches it.
  dynamic "statement" {
    for_each = length(local.runtime_arns) > 0 ? [1] : []
    content {
      sid     = "InvokeAgentRuntime"
      actions = ["bedrock-agentcore:InvokeAgentRuntime"]
      # AgentCore enforces InvokeAgentRuntime against the *endpoint* ARN
      # (``…/runtime/{name}/runtime-endpoint/{qualifier}``), not the bare
      # runtime ARN. Granting both the runtime and its ``/runtime-endpoint/*``
      # children covers every qualifier (``DEFAULT`` today, others later).
      resources = concat(
        local.runtime_arns,
        [for arn in local.runtime_arns : "${arn}/runtime-endpoint/*"],
      )
    }
  }

  statement {
    sid       = "PutEventsOnBus"
    actions   = ["events:PutEvents"]
    resources = [var.bus_arn]
  }

  statement {
    sid       = "WriteRunsTable"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [var.runs_table_arn]
  }

  statement {
    sid       = "InvokeHitl"
    actions   = ["lambda:InvokeFunction"]
    resources = [module.hitl_handler.lambda_function_arn]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}
