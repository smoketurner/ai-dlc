################################################################################
# ECR repositories for agent + dashboard container images.
#
# Tags are immutable EXCEPT for "latest", which the build workflows
# overwrite on every push to main. SHA-tagged images (the actual
# digest-pinned references terraform consumes via data.aws_ecr_image)
# remain immutable. Lifecycle expires untagged images after a week and
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
