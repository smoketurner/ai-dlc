################################################################################
# AgentCore Runtime — one per agent.
#
# Each runtime gets its own IAM role with the minimum it needs:
#   * ECR pull on the agent's repository
#   * bedrock:InvokeModel on the agent's chosen Claude model
#   * S3 read/write on the artifacts + memory_md prefixes that match the project
#   * AgentCore Memory CreateEvent / Retrieve / ListEvents on this env's memory
#   * CloudWatch Logs write
#
# Image deploys are handled out-of-band by the images-build workflow:
# after pushing a new image to ECR, the workflow runs
#   aws bedrock-agentcore-control update-agent-runtime --container-uri ...
# which atomically replaces the running container. terraform reads the
# initial digest from `:latest` at create time so the runtime can be
# bootstrapped, then ignores subsequent changes via lifecycle.
################################################################################

data "aws_iam_policy_document" "runtime_assume" {
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

data "aws_ecr_image" "agent" {
  for_each = var.agents

  repository_name = "${var.project}/${each.key}"
  image_tag       = "latest"
}

data "aws_iam_policy_document" "runtime_inline" {
  for_each = var.agents

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      "arn:${local.aws_partition}:ecr:${local.aws_region}:${local.aws_account_id}:repository/${var.project}/${each.key}",
    ]
  }

  statement {
    sid     = "BedrockInvokeModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:${local.aws_partition}:bedrock:*::foundation-model/*",
      "arn:${local.aws_partition}:bedrock:*:${local.aws_account_id}:inference-profile/*",
    ]
  }

  statement {
    sid    = "S3Artifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      var.artifacts_bucket_arn,
      "${var.artifacts_bucket_arn}/*",
      var.memory_md_bucket_arn,
      "${var.memory_md_bucket_arn}/*",
    ]
  }

  statement {
    sid = "AgentCoreMemory"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:RetrieveMemoryRecords",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:GetMemory",
    ]
    resources = [aws_bedrockagentcore_memory.this.arn]
  }

  statement {
    sid = "AgentCoreGateway"
    actions = [
      "bedrock-agentcore:InvokeGateway",
      "bedrock-agentcore:GetGateway",
      "bedrock-agentcore:ListGatewayTargets",
    ]
    resources = [aws_bedrockagentcore_gateway.agent[each.key].gateway_arn]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:${local.aws_partition}:logs:*:${local.aws_account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
}

resource "aws_iam_role" "runtime" {
  for_each = var.agents

  name               = "${local.prefix}-${each.key}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
  description        = "Execution role for the ${each.key} AgentCore Runtime."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-runtime"
    Component = "agents"
  })
}

resource "aws_iam_role_policy" "runtime_inline" {
  for_each = var.agents

  name   = "runtime-inline"
  role   = aws_iam_role.runtime[each.key].id
  policy = data.aws_iam_policy_document.runtime_inline[each.key].json
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  for_each = var.agents

  agent_runtime_name = replace("${local.prefix}-${each.key}", "-", "_")
  description        = each.value.description
  role_arn           = aws_iam_role.runtime[each.key].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${var.ecr_repository_urls[each.key]}@${data.aws_ecr_image.agent[each.key].image_digest}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = var.cognito_discovery_url
      allowed_audience = var.cognito_audience
    }
  }

  environment_variables = {
    AIDLC_ENV               = var.env
    AIDLC_ARTIFACTS_BUCKET  = var.artifacts_bucket
    AIDLC_MEMORY_MD_BUCKET  = var.memory_md_bucket
    AIDLC_MEMORY_ID         = aws_bedrockagentcore_memory.this.id
    AIDLC_AGENT_GATEWAY_URL = aws_bedrockagentcore_gateway.agent[each.key].gateway_url
    AIDLC_BEDROCK_MODEL_ID  = each.value.bedrock_model_id
  }

  # The images-build workflow updates the container URI out-of-band via
  # `aws bedrock-agentcore-control update-agent-runtime`. Let those updates
  # stand without terraform reverting them on the next apply.
  lifecycle {
    ignore_changes = [agent_runtime_artifact]
  }

  tags = merge(var.tags, {
    Name      = replace("${local.prefix}-${each.key}", "-", "_")
    Component = "agents"
  })
}
