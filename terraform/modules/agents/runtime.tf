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

# Look up the repo URL via a data source instead of relying on
# ``var.ecr_repository_urls`` so agents whose ECR repo was auto-created
# by the registry module's repository_creation_template (and therefore
# not in the registry module's explicit ``var.repositories`` set) still
# resolve cleanly.
data "aws_ecr_repository" "agent" {
  for_each = var.agent_image_tags

  name = "${var.project}/${each.key}"
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

  # Bedrock-Marketplace prerequisite: ConverseStream against Anthropic
  # models triggers a per-principal subscription check. Without these
  # perms the runtime role gets ``AccessDeniedException ... AWS Marketplace
  # subscription for this model cannot be completed`` on first use, even
  # when the account is already subscribed via the console under a
  # different IAM principal. Scoped to ViewSubscriptions / Subscribe;
  # no Unsubscribe (the runtime should never tear down access).
  statement {
    sid = "BedrockMarketplaceSubscribe"
    actions = [
      "aws-marketplace:ViewSubscriptions",
      "aws-marketplace:Subscribe",
    ]
    resources = ["*"]
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

  # Direct S3 access for the two agents whose paths sit outside the
  # gateway:
  #   * architect — pre-agent clone-sync writes MEMORY.md +
  #     stack_profile.json to ``memory_md_bucket`` (see
  #     ``repo_grounding.sync_memory_md_from_clone`` /
  #     ``common.memory_md.write_stack_profile``). GetObject backs the
  #     content-MD5 idempotency check that skips re-puts of unchanged
  #     bodies.
  #   * triage — writes the run's triage decision JSON to
  #     ``artifacts_bucket``. No gateway targets.
  # Every other agent reaches S3 through the gateway's ``artifact_tool``.
  dynamic "statement" {
    for_each = each.key == "architect" ? [1] : []
    content {
      sid    = "S3MemoryMd"
      effect = "Allow"
      actions = [
        "s3:GetObject",
        "s3:PutObject",
      ]
      resources = [
        var.memory_md_bucket_arn,
        "${var.memory_md_bucket_arn}/*",
      ]
    }
  }

  dynamic "statement" {
    for_each = each.key == "triage" ? [1] : []
    content {
      sid    = "S3TriageDecision"
      effect = "Allow"
      actions = [
        "s3:PutObject",
      ]
      resources = [
        "${var.artifacts_bucket_arn}/*",
      ]
    }
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

  # AgentCore Identity M2M token exchange — every agent fetches a
  # Cognito M2M JWT via this API to authenticate against its own
  # gateway. ``GetResourceOauth2Token`` resolves the credential
  # provider name to its stored client_id/secret and runs the
  # client_credentials flow against Cognito. Scoped to wildcard
  # because the API itself enforces the workload identity that owns
  # the call.
  statement {
    sid       = "AgentCoreGatewayM2MToken"
    actions   = ["bedrock-agentcore:GetResourceOauth2Token"]
    resources = ["*"]
  }

  # AgentCore Identity stores the M2M (and GitHub) provider client
  # secrets in its private Secrets Manager vault prefix. The runtime
  # needs read access to the vault entry so the M2M exchange can
  # complete.
  statement {
    sid       = "ReadAgentCoreIdentityVault"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:${local.aws_partition}:secretsmanager:*:*:secret:bedrock-agentcore-identity!default/*"]
  }

  # AgentCore Browser — resource-bound lifecycle / session-management
  # actions. Scoped to the shared browser ARN and the derived session
  # ARNs underneath it.
  dynamic "statement" {
    for_each = contains(each.value.features, "browser") ? [1] : []
    content {
      sid = "AgentCoreBrowser"
      actions = [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:ListBrowserSessions",
        "bedrock-agentcore:UpdateBrowserStream",
      ]
      resources = [
        aws_bedrockagentcore_browser.shared.browser_arn,
        "${aws_bedrockagentcore_browser.shared.browser_arn}/*",
      ]
    }
  }

  # AgentCore Browser — automation-stream connect. The IAM action takes
  # no resource constraint per AWS service-authorization docs, so it
  # must use ``Resource = "*"``. Without this, the WSS handshake against
  # the browser-streams endpoint fails with 403 even when the rest of
  # the browser policy is in place.
  dynamic "statement" {
    for_each = contains(each.value.features, "browser") ? [1] : []
    content {
      sid       = "AgentCoreBrowserStream"
      actions   = ["bedrock-agentcore:ConnectBrowserAutomationStream"]
      resources = ["*"]
    }
  }

  # AgentCore Code Interpreter. Granted to agents whose ``features``
  # include ``code_interpreter`` (currently Tester + Reviewer for
  # sandboxed test/lint execution).
  dynamic "statement" {
    for_each = contains(each.value.features, "code_interpreter") ? [1] : []
    content {
      sid = "AgentCoreCodeInterpreter"
      actions = [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:StopCodeInterpreterSession",
        "bedrock-agentcore:GetCodeInterpreterSession",
        "bedrock-agentcore:ListCodeInterpreterSessions",
        "bedrock-agentcore:InvokeCodeInterpreter",
      ]
      resources = [
        aws_bedrockagentcore_code_interpreter.shared.code_interpreter_arn,
        "${aws_bedrockagentcore_code_interpreter.shared.code_interpreter_arn}/*",
      ]
    }
  }

  # Direct lambda:InvokeFunction on the repo_helper Lambda for the
  # three agents whose tool list includes ``common.sandbox.get_pr_diff``
  # or ``run_pr_in_sandbox`` — both helpers invoke ``repo_helper``
  # directly to fetch the PR diff / a short-lived archive URL for the
  # Code Interpreter sandbox. All other agents reach repo_helper through
  # the gateway.
  dynamic "statement" {
    for_each = contains(["reviewer", "tester", "code_critic"], each.key) ? [1] : []
    content {
      sid       = "SandboxInvokeRepoHelper"
      actions   = ["lambda:InvokeFunction"]
      resources = [module.tool_lambda["repo_helper"].lambda_function_arn]
    }
  }

  # AgentCore Identity user-OBO. Granted to agents whose ``targets``
  # include ``repo_helper`` — these are the agents that may need to
  # mint user-on-behalf-of GitHub tokens directly (Implementer for
  # git CLI auth). The wildcard resource is intentional: AgentCore
  # itself enforces scope via the workload identity + credential
  # provider configuration. ``GetResourceOauth2Token`` lives in the
  # universal ``AgentCoreGatewayM2MToken`` statement above; this
  # statement only adds the OBO-specific workload exchanges.
  dynamic "statement" {
    for_each = contains(each.value.targets, "repo_helper") && var.github_app_secret_name != null ? [1] : []
    content {
      sid = "AgentCoreUserObo"
      actions = [
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      ]
      resources = ["*"]
    }
  }

  # Architect + implementer mint GitHub installation tokens directly
  # inside their containers (architect for repo cloning during spec
  # grounding; implementer for clone / commit / push). Both need read
  # access to the App's private-key secret in Secrets Manager.
  dynamic "statement" {
    for_each = contains(local.github_app_direct_agents, each.key) && var.github_app_secret_name != null ? [1] : []
    content {
      sid       = "ReadGithubAppSecret"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [data.aws_secretsmanager_secret.github_app[0].arn]
    }
  }

  # Every agent emits its own completion event (``DESIGN.READY``,
  # ``IMPL_PR.OPENED``, ``REVIEW.READY``, ``TEST_REPORT.READY``,
  # ``CODE_CRITIQUE.READY``, ``REVISION.READY``, ``ISSUE.TRIAGED``)
  # onto the platform bus when finished. The state-router invocation is
  # fire-and-forget; the agent's event is what advances the projector's
  # state machine.
  statement {
    sid       = "EventBusPublish"
    actions   = ["events:PutEvents"]
    resources = [var.bus_arn]
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
      container_uri = "${data.aws_ecr_repository.agent[each.key].repository_url}@${data.aws_ecr_image.agent[each.key].image_digest}"
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
      #
      # Opt into the stable OpenTelemetry GenAI semantic conventions so
      # Strands emits ``gen_ai.*`` attributes plus per-tool definition
      # spans on the existing trace pipeline. Without this Strands falls
      # back to its older convention set, which omits the tool-definition
      # attributes we use to slice latency / cost by tool in CloudWatch.
      OTEL_SEMCONV_STABILITY_OPT_IN = "gen_ai_latest_experimental,gen_ai_tool_definitions"
      AIDLC_ARTIFACTS_BUCKET        = var.artifacts_bucket
      AIDLC_MEMORY_MD_BUCKET        = var.memory_md_bucket
      AIDLC_MEMORY_ID               = aws_bedrockagentcore_memory.this.id
      AIDLC_AGENT_GATEWAY_URL       = aws_bedrockagentcore_gateway.agent[each.key].gateway_url
      AIDLC_BEDROCK_MODEL_ID        = var.agents[each.key].bedrock_model_id

      # AgentCore Identity M2M provider that mints the Cognito JWT the
      # agent uses as the Bearer when calling its gateway. Scope is the
      # full ``<resource>/<scope>`` string Cognito's token endpoint
      # expects in the ``scope`` parameter.
      AIDLC_GATEWAY_OAUTH_PROVIDER_NAME = aws_bedrockagentcore_oauth2_credential_provider.cognito_gateway_m2m.name
      AIDLC_GATEWAY_OAUTH_SCOPE         = var.cognito_gateway_m2m_scope
    },
    # Every agent emits its completion event on the platform bus when
    # finished — wire the bus name into the runtime env so each container
    # can call ``events:PutEvents`` without a per-agent override.
    {
      AIDLC_BUS_NAME = var.bus_name
    },
    # Optional fallback Bedrock model. When set, the Strands agents'
    # `common.runtime.invoke_with_fallback` rebuilds the agent on this
    # model after a `ModelThrottledException` from the primary. Skip the
    # env var entirely when unset so the agent's `os.environ.get` returns
    # `None` and the helper short-circuits.
    var.agents[each.key].bedrock_fallback_model_id != "" ? {
      AIDLC_BEDROCK_FALLBACK_MODEL_ID = var.agents[each.key].bedrock_fallback_model_id
    } : {},
    contains(var.agents[each.key].targets, "repo_helper") ? {
      AIDLC_REPO_HELPER_FUNCTION_NAME = module.tool_lambda["repo_helper"].lambda_function_name
    } : {},
    contains(var.agents[each.key].targets, "repo_helper") && var.github_app_secret_name != null ? {
      AIDLC_GITHUB_OAUTH_PROVIDER_NAME = aws_bedrockagentcore_oauth2_credential_provider.github[0].name
      AIDLC_AGENT_WORKLOAD_NAME        = aws_bedrockagentcore_workload_identity.platform[0].name
    } : {},
    # Architect + implementer mint GitHub installation tokens directly
    # inside their containers — architect for repo cloning during spec
    # grounding (``repo_grounding.clone_target_repo``), implementer for
    # clone / commit / push. Both need the App's secret ARN. Other
    # agents delegate git ops to the repo_helper Lambda.
    contains(local.github_app_direct_agents, each.key) && var.github_app_secret_name != null ? {
      AIDLC_GITHUB_APP_SECRET_ARN = data.aws_secretsmanager_secret.github_app[0].arn
    } : {},
    contains(var.agents[each.key].features, "browser") ? {
      AIDLC_BROWSER_ID = aws_bedrockagentcore_browser.shared.browser_id
    } : {},
    contains(var.agents[each.key].features, "code_interpreter") ? {
      AIDLC_CODE_INTERPRETER_ID = aws_bedrockagentcore_code_interpreter.shared.code_interpreter_id
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
