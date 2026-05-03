data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

data "aws_iam_policy" "memory_inference" {
  name = "AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy"
}

# Trust policy for the AgentCore Memory service execution role.
data "aws_iam_policy_document" "memory_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
  }
}

# Trust policy reused by every per-agent AgentCore Gateway role.
data "aws_iam_policy_document" "gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.aws_account_id]
    }
  }
}

# Permissions granted to each per-agent gateway role: invoke the tool Lambdas
# the agent is allowed to call.
data "aws_iam_policy_document" "gateway_invoke" {
  for_each = var.agents

  statement {
    sid       = "InvokeToolLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [for tool in each.value.targets : module.tool_lambda[tool].lambda_function_arn]
  }
}
