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
    # Per AWS samples (sample-strands-agent-with-agentcore et al), the
    # runtime trust policy is a bare service-principal allow — no
    # ``aws:SourceAccount`` condition. AgentCore appears to not pass the
    # source-account context when assuming the role, which silently
    # blocks assumption when the condition is present.
  }
}

data "aws_ecr_image" "agent" {
  for_each = var.agent_image_tags

  repository_name = "${var.project}/${each.key}"
  image_tag       = each.value
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
    sid = "BedrockInvokeModel"
    # Strands' BedrockModel uses the Converse API, not raw InvokeModel.
    # Both the Converse and InvokeModel families need to be on the role
    # so the agent works regardless of which Strands code path is hit.
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      "arn:${local.aws_partition}:bedrock:*::foundation-model/*",
      "arn:${local.aws_partition}:bedrock:*:${local.aws_account_id}:inference-profile/*",
    ]
  }

  # ADOT auto-instrumentation in the AgentCore runtime emits X-Ray
  # segments + CloudWatch metrics. Without these perms the OTEL exporter
  # blocks on permission errors at startup, which the runtime surfaces
  # as a generic 500.
  statement {
    sid = "Telemetry"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
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

  # Direct lambda:InvokeFunction on the agent's tool targets. Most agents
  # call tools via the gateway (which has its own role) — this is the
  # escape hatch for agents that orchestrate Lambdas directly (e.g., the
  # Proposer calling repo_helper to commit + open a PR). The set is
  # already bounded by `targets`, so least-privilege still holds.
  dynamic "statement" {
    for_each = length(each.value.targets) > 0 ? [1] : []
    content {
      sid       = "DirectInvokeToolLambdas"
      actions   = ["lambda:InvokeFunction"]
      resources = [for tool in each.value.targets : module.tool_lambda[tool].lambda_function_arn]
    }
  }

  # AgentCore Identity user-OBO. Granted to agents whose ``targets``
  # include ``repo_helper`` — these are the agents that may need to
  # mint user-on-behalf-of GitHub tokens directly (Implementer for
  # git CLI auth). The wildcard resource is intentional: AgentCore
  # itself enforces scope via the workload identity + credential
  # provider configuration.
  dynamic "statement" {
    for_each = contains(each.value.targets, "repo_helper") && var.github_app_secret_name != null ? [1] : []
    content {
      sid = "AgentCoreUserObo"
      actions = [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetResourceOauth2Token",
      ]
      resources = ["*"]
    }
  }

  # AgentCore Identity uses Forward-Access Session to read its own
  # internal credential-vault secret (the user's cached GitHub OAuth
  # token, keyed by Cognito sub). Without this perm, GetResourceOauth2Token
  # raises AccessDeniedException at the secretsmanager layer.
  dynamic "statement" {
    for_each = contains(each.value.targets, "repo_helper") && var.github_app_secret_name != null ? [1] : []
    content {
      sid       = "ReadAgentCoreIdentitySecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = ["arn:${local.aws_partition}:secretsmanager:*:*:secret:bedrock-agentcore-identity!default/*"]
    }
  }

  # Implementer-only: read the GitHub App credentials secret so the
  # container can mint installation tokens for in-container git operations.
  dynamic "statement" {
    for_each = each.key == "implementer" && var.github_app_secret_name != null ? [1] : []
    content {
      sid       = "ReadGithubAppSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [data.aws_secretsmanager_secret.github_app[0].arn]
    }
  }

  # Task-phase agents (Implementer, Reviewer, Tester) report completion
  # back to Step Functions directly via SendTaskSuccess/Failure once
  # they finish — the InvokeAgentRuntime call from runtime_invoker is
  # fire-and-forget. Without this perm the agent succeeds but SF stays
  # blocked on the task token forever.
  dynamic "statement" {
    for_each = contains(["implementer", "reviewer", "tester"], each.key) ? [1] : []
    content {
      sid = "StatesTaskCallback"
      actions = [
        "states:SendTaskSuccess",
        "states:SendTaskFailure",
        "states:SendTaskHeartbeat",
      ]
      resources = ["*"]
    }
  }

  statement {
    sid = "Logs"
    # AgentCore Runtime creates ``/aws/bedrock-agentcore/runtimes/{id}-{qualifier}``
    # for OTEL traces and may also create ``application-logs`` / similar streams
    # under a sibling group. Granting both ``log-group:`` and the ``log-stream``
    # children of any ``/aws/bedrock-agentcore/*`` group covers every shape
    # without leaking log access outside the AgentCore namespace.
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "logs:DescribeLogGroups",
    ]
    resources = [
      "arn:${local.aws_partition}:logs:*:${local.aws_account_id}:log-group:/aws/bedrock-agentcore/*",
      "arn:${local.aws_partition}:logs:*:${local.aws_account_id}:log-group:/aws/bedrock-agentcore/*:log-stream:*",
    ]
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
  for_each = var.agent_image_tags

  agent_runtime_name = replace("${local.prefix}-${each.key}", "-", "_")
  description        = var.agents[each.key].description
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

  # Auth is IAM/SigV4. Step Functions invokes via the native SDK
  # integration (``aws-sdk:bedrockagentcore:invokeAgentRuntime``) signed
  # by its execution role — no Cognito JWT round-trip. Direct invokers
  # need ``bedrock-agentcore:InvokeAgentRuntime`` on the runtime's
  # endpoint ARN. Omitting ``authorizer_configuration`` selects IAM auth.

  environment_variables = merge(
    {
      # boto3 inside the agent container needs ``AWS_REGION`` to reach
      # the AgentCore-injected credential metadata endpoint and to sign
      # Bedrock requests. Without it the default-creds chain fails with
      # ``NoCredentialsError``.
      AWS_REGION = local.aws_region
      AIDLC_ENV  = var.env
      # NOTE: do NOT set ``OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=false``.
      # AgentCore relies on the bedrock-agentcore SDK's bundled (deprecated)
      # LoggingHandler to forward structlog/stdlib log output through the
      # OTEL pipeline into CloudWatch. Disabling it silences the deprecation
      # warning at startup but also drops every application log line —
      # ``opentelemetry-instrumentation-logging`` only handles trace
      # correlation, not log forwarding. Live with the warning until AWS
      # ships their replacement handler.
      AIDLC_ARTIFACTS_BUCKET  = var.artifacts_bucket
      AIDLC_MEMORY_MD_BUCKET  = var.memory_md_bucket
      AIDLC_MEMORY_ID         = aws_bedrockagentcore_memory.this.id
      AIDLC_AGENT_GATEWAY_URL = aws_bedrockagentcore_gateway.agent[each.key].gateway_url
      AIDLC_BEDROCK_MODEL_ID  = var.agents[each.key].bedrock_model_id
    },
    contains(var.agents[each.key].targets, "repo_helper") ? {
      AIDLC_REPO_HELPER_FUNCTION_NAME = module.tool_lambda["repo_helper"].lambda_function_name
    } : {},
    contains(var.agents[each.key].targets, "repo_helper") && var.github_app_secret_name != null ? {
      AIDLC_GITHUB_OAUTH_PROVIDER_NAME = aws_bedrockagentcore_oauth2_credential_provider.github[0].name
      AIDLC_AGENT_WORKLOAD_NAME        = aws_bedrockagentcore_workload_identity.agent[each.key].name
    } : {},
    # Implementer-only: it does git operations directly inside its container
    # (clone / commit / push), so it needs the App's installation token —
    # minted from the App private key in Secrets Manager. Other agents
    # delegate git ops to the repo_helper Lambda and don't need this.
    each.key == "implementer" && var.github_app_secret_name != null ? {
      AIDLC_GITHUB_APP_SECRET_ARN = data.aws_secretsmanager_secret.github_app[0].arn
    } : {},
  )

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
