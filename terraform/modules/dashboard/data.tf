data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy" "ecs_task_execution" {
  name = "AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "task_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.aws_account_id]
    }
  }
}

data "aws_iam_policy_document" "task_inline" {
  statement {
    sid = "DynamoDB"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.runs_table_arn,
      "${var.runs_table_arn}/index/*",
      var.approvals_table_arn,
      "${var.approvals_table_arn}/index/*",
      var.idempotency_table_arn,
    ]
  }

  statement {
    sid = "DynamoDBDelete"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [
      var.runs_table_arn,
      var.approvals_table_arn,
    ]
  }

  statement {
    sid       = "PutEvents"
    actions   = ["events:PutEvents"]
    resources = [var.bus_arn]
  }

  statement {
    sid       = "InvokeHitl"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.hitl_handler_function_arn]
  }

  statement {
    sid       = "InvokeTriageDispatcher"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.triage_dispatcher_function_arn]
  }

  statement {
    sid       = "ReadGithubAppSecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [var.github_app_secret_arn]
  }

  statement {
    sid       = "ReadWebhookSecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [var.github_webhook_secret_arn]
  }

  statement {
    sid     = "ReadArtifacts"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      var.artifacts_bucket_arn,
      "${var.artifacts_bucket_arn}/*",
    ]
  }

  # AgentCore Identity user-OBO flow — used by /auth/github to bridge the
  # Cognito-authenticated user into AgentCore's USER_FEDERATION on the
  # GithubOauth2 credential provider. Empty when github_app isn't
  # configured; AgentCore-side enforcement bounds these by workload
  # identity + credential provider, so the wildcard resource is safe.
  dynamic "statement" {
    for_each = var.dashboard_workload_name == "" ? [] : [1]
    content {
      sid = "AgentCoreUserObo"
      actions = [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetResourceOauth2Token",
        "bedrock-agentcore:CompleteResourceTokenAuth",
      ]
      resources = ["*"]
    }
  }

  # AgentCore Identity reads its own internal Secrets Manager secret to
  # retrieve cached OAuth tokens. The call is made via Forward-Access
  # Session, so the caller (dashboard task role) must hold the permission
  # — AgentCore's own service role isn't enough. The secret name is
  # service-managed and follows the pattern below.
  dynamic "statement" {
    for_each = var.dashboard_workload_name == "" ? [] : [1]
    content {
      sid       = "ReadAgentCoreIdentitySecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = ["arn:aws:secretsmanager:*:*:secret:bedrock-agentcore-identity!default/*"]
    }
  }
}

data "aws_iam_policy_document" "execution_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}
