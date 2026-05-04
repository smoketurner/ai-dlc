data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy" "administrator_access" {
  name = "AdministratorAccess"
}

data "aws_iam_policy_document" "terraform_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = concat([local.pr_subject], local.branch_subjects_tf)
    }
  }
}

data "aws_iam_policy_document" "image_publisher_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.branch_subjects_image
    }
  }
}

data "aws_iam_policy_document" "terraform_inline" {
  statement {
    sid    = "ReadOnlyForPlan"
    effect = "Allow"

    actions = [
      "ec2:Describe*",
      "iam:Get*",
      "iam:List*",
      "kms:Describe*",
      "kms:List*",
      "s3:GetBucket*",
      "s3:GetObject*",
      "s3:ListBucket",
      "s3:ListAllMyBuckets",
      "dynamodb:Describe*",
      "dynamodb:List*",
      "events:Describe*",
      "events:List*",
      "sqs:GetQueueAttributes",
      "sqs:ListQueues",
      "states:Describe*",
      "states:List*",
      "lambda:Get*",
      "lambda:List*",
      "ecr:Describe*",
      "ecr:List*",
      "logs:Describe*",
      "logs:List*",
      "cloudwatch:Describe*",
      "cloudwatch:List*",
      "sns:Get*",
      "sns:List*",
      "cognito-idp:Describe*",
      "cognito-idp:List*",
      "elasticloadbalancing:Describe*",
      "ecs:Describe*",
      "ecs:List*",
      "bedrock:List*",
      "bedrock:Get*",
      "bedrock-agentcore:Get*",
      "bedrock-agentcore:List*",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecrets",
      "schemas:Describe*",
      "schemas:List*",
    ]

    resources = ["*"]
  }
}

data "aws_iam_policy_document" "image_publisher_inline" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
    ]

    resources = [
      "arn:${local.aws_partition}:ecr:*:${local.aws_account_id}:repository/${var.project}/*",
    ]
  }

  # Post-push deploys: dashboard-build calls ecs:UpdateService to roll the
  # ECS service onto the new :latest digest; images-build looks up the
  # AgentCore Runtime ID by name and calls update-agent-runtime.
  statement {
    sid     = "EcsRollDashboard"
    actions = ["ecs:UpdateService", "ecs:DescribeServices"]
    resources = [
      "arn:${local.aws_partition}:ecs:*:${local.aws_account_id}:service/${var.project}-*-dashboard/${var.project}-*-dashboard",
    ]
  }

  statement {
    sid = "AgentCoreRollRuntime"
    # IAM actions for AgentCore live under the ``bedrock-agentcore:``
    # prefix even though the CLI command is ``bedrock-agentcore-control``.
    actions = [
      "bedrock-agentcore:ListAgentRuntimes",
      "bedrock-agentcore:GetAgentRuntime",
      "bedrock-agentcore:UpdateAgentRuntime",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "evals_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = concat([local.pr_subject], local.branch_subjects_evals)
    }
  }
}

data "aws_iam_policy_document" "evals_inline" {
  statement {
    sid       = "SyncEvalCases"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["arn:${local.aws_partition}:s3:::${var.project}-*-artifacts-${local.aws_account_id}-*/evals/*"]
  }

  statement {
    sid       = "StartEvalRun"
    actions   = ["states:StartExecution"]
    resources = ["arn:${local.aws_partition}:states:*:${local.aws_account_id}:stateMachine:${var.project}-*-eval-runner"]
  }

  statement {
    sid = "WatchEvalRun"
    actions = [
      "states:DescribeExecution",
      "states:GetExecutionHistory",
      "states:StopExecution",
    ]
    resources = ["arn:${local.aws_partition}:states:*:${local.aws_account_id}:execution:${var.project}-*-eval-runner:*"]
  }
}
