################################################################################
# ECR repositories for agent + dashboard container images.
#
# IMMUTABLE tags + scan-on-push. Each commit produces a single uniquely-
# tagged (:<git-sha>) image; terraform-apply queries ECR for the most-
# recently-pushed tag per repo and pins by digest via data.aws_ecr_image.
# Lifecycle expires untagged images after a week and keeps the most recent
# N tagged images per repo. AgentCore Runtime is granted Pull on the agent
# repos via repository policy.
################################################################################

resource "aws_ecr_repository" "this" {
  for_each = var.repositories

  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "IMMUTABLE"

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
