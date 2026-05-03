data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy" "ecs_task_execution" {
  name = "AmazonECSTaskExecutionRolePolicy"
}


data "aws_ecr_image" "dashboard" {
  count = local.has_image ? 1 : 0

  repository_name = "${var.project}/dashboard"
  image_tag       = var.image_tag
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
    sid       = "ReadWebhookSecret"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [var.github_webhook_secret_arn]
  }

  statement {
    sid     = "ReadArtifacts"
    actions = ["s3:GetObject"]
    resources = [
      "${var.artifacts_bucket_arn}/*",
    ]
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
