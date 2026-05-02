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
  statement {
    sid       = "InvokeAgentRuntime"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = compact([local.architect_runtime_arn, local.implementer_runtime_arn])
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
