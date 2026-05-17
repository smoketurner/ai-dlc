################################################################################
# ECR repositories for agent + dashboard container images.
#
# Tags are immutable EXCEPT for "latest", which build workflows overwrite on
# every push to main. SHA-tagged images stay immutable, so any commit can be
# resurrected by digest. Lifecycle expires untagged images after a week and
# keeps the most recent N tagged images per repo. AgentCore Runtime is
# granted Pull on the agent repos via repository policy.
################################################################################

resource "aws_ecr_repository" "this" {
  for_each = var.repositories

  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"

  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.tags, {
    Name      = "${var.project}/${each.key}"
    Component = "registry"
  })
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_image_retention_days
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last N tagged"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = var.tagged_image_retention_count
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_ecr_repository_policy" "agentcore_pull" {
  for_each = {
    for k, v in aws_ecr_repository.this : k => v
    if contains(var.agentcore_pull_repositories, k)
  }

  repository = each.value.name
  policy     = data.aws_iam_policy_document.agentcore_pull.json
}

################################################################################
# Repository creation template — safety net for first-push-before-terraform.
#
# ``var.repositories`` is the source of truth: every agent ECR repo should be
# listed there so it gets an explicit ``aws_ecr_repository`` +
# ``aws_ecr_lifecycle_policy`` + ``aws_ecr_repository_policy`` managed by
# terraform. If an image is pushed to ``${project}/<name>`` before the repo
# is declared, this template auto-creates it with the same mutability,
# lifecycle, and AgentCore-pull policy so the new repo isn't left in a
# weaker state than the declared ones.
#
# Important: ECR applies a creation template only at repo-create time. It
# does NOT retroactively reconcile policy on repos that already exist — so
# bumping ``var.tagged_image_retention_count`` here will not change
# lifecycle on previously auto-created repos. The fix in that case is to
# add the repo to ``var.repositories`` and ``terraform import`` it, which
# brings it under the explicit ``aws_ecr_lifecycle_policy.this`` fan-out.
################################################################################

data "aws_iam_policy_document" "ecr_create_on_push_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecr.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecr_create_on_push" {
  name               = "${var.project}-ecr-create-on-push"
  assume_role_policy = data.aws_iam_policy_document.ecr_create_on_push_assume.json

  tags = merge(var.tags, {
    Name      = "${var.project}-ecr-create-on-push"
    Component = "registry"
  })
}

resource "aws_iam_role_policy" "ecr_create_on_push" {
  name = "${var.project}-ecr-create-on-push"
  role = aws_iam_role.ecr_create_on_push.id

  # Canonical permissions per
  # https://docs.aws.amazon.com/AmazonECR/latest/userguide/repository-creation-templates-custom.html
  # KMS permissions (kms:CreateGrant / RetireGrant / DescribeKey) are
  # omitted because the template uses ``encryption_type = "AES256"``.
  # Add them back if the encryption type ever switches to KMS.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:CreateRepository",
          "ecr:ReplicateImage",
          "ecr:TagResource",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_ecr_repository_creation_template" "agents" {
  prefix      = var.project
  description = "Auto-create ${var.project}/<name> repos on first push with the standard agent config."

  applied_for = ["CREATE_ON_PUSH"]

  custom_role_arn      = aws_iam_role.ecr_create_on_push.arn
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"

  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_image_retention_days
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last N tagged"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = var.tagged_image_retention_count
        }
        action = { type = "expire" }
      },
    ]
  })

  repository_policy = data.aws_iam_policy_document.agentcore_pull.json

  resource_tags = merge(var.tags, {
    Component = "registry"
    CreatedBy = "ecr-creation-template"
  })
}
