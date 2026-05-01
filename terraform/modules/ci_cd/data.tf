data "aws_caller_identity" "current" {}

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
      "bedrock-agentcore-control:Get*",
      "bedrock-agentcore-control:List*",
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
      "arn:aws:ecr:*:${data.aws_caller_identity.current.account_id}:repository/${var.project}/*",
    ]
  }
}
