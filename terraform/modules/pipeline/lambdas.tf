################################################################################
# Platform Lambdas — entry_adapter, hitl_handler, event_projector.
#
# Each Lambda is built from `lambdas/<name>/` via the terraform-aws-modules
# wrapper (zip + pip-install dependencies in Docker for arm64). IAM is
# scoped narrowly to the resources each function actually touches.
################################################################################

module "entry_adapter" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-entry-adapter"
  description   = "POST /v1/runs → idempotency check → events:PutEvents REQUEST.RECEIVED."
  handler       = "entry_adapter.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 256
  timeout       = 10
  publish       = true
  tracing_mode  = "Active"

  source_path = [{
    path             = "${local.source_dir}/entry_adapter/src"
    pip_requirements = "${local.source_dir}/entry_adapter/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_BUS_NAME                = var.bus_name
    AIDLC_IDEMPOTENCY_TABLE       = var.idempotency_table
    AIDLC_IDEMPOTENCY_TTL         = "86400"
    POWERTOOLS_SERVICE_NAME       = "entry_adapter"
    POWERTOOLS_METRICS_NAMESPACE  = "ai-dlc"
    POWERTOOLS_LOG_LEVEL          = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT   = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    idempotency_table = {
      effect = "Allow"
      # Powertools' DynamoDBPersistenceLayer needs UpdateItem in addition
      # to PutItem/GetItem to flip in-progress records to completed.
      actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
      resources = [var.idempotency_table_arn]
    }
    put_events = {
      effect    = "Allow"
      actions   = ["events:PutEvents"]
      resources = [var.bus_arn]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-entry-adapter"
    Component = "pipeline"
  })
}

module "hitl_handler" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-hitl-handler"
  description   = "REQUEST_APPROVAL (.waitForTaskToken caller) + DECIDE (resolve gate)."
  handler       = "hitl_handler.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 256
  timeout       = 30
  publish       = true
  tracing_mode  = "Active"

  source_path = [{
    path             = "${local.source_dir}/hitl_handler/src"
    pip_requirements = "${local.source_dir}/hitl_handler/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_APPROVALS_TABLE         = var.approvals_table
    POWERTOOLS_SERVICE_NAME       = "hitl_handler"
    POWERTOOLS_METRICS_NAMESPACE  = "ai-dlc"
    POWERTOOLS_LOG_LEVEL          = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT   = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    approvals_table = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        # Query is required by the CANCEL_RUN op: list every PENDING
        # GATE row for a run and SendTaskFailure on each task_token.
        "dynamodb:Query",
      ]
      resources = [var.approvals_table_arn]
    }
    states_callback = {
      effect    = "Allow"
      actions   = ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"]
      resources = ["*"]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-hitl-handler"
    Component = "pipeline"
  })
}

module "triage_dispatcher" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-triage-dispatcher"
  description   = "GitHub issue → triage agent runtime → REQUEST.RECEIVED or comment+label."
  handler       = "triage_dispatcher.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60
  publish       = true
  tracing_mode  = "Active"

  source_path = [{
    path             = "${local.source_dir}/triage_dispatcher/src"
    pip_requirements = "${local.source_dir}/triage_dispatcher/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_BUS_NAME                  = var.bus_name
    AIDLC_REPO_HELPER_FUNCTION_NAME = var.repo_helper_function_name
    AIDLC_TRIAGE_RUNTIME_ARN        = var.triage_runtime_arn
    AIDLC_ARTIFACTS_BUCKET          = var.artifacts_bucket
    POWERTOOLS_SERVICE_NAME         = "triage_dispatcher"
    POWERTOOLS_METRICS_NAMESPACE    = "ai-dlc"
    POWERTOOLS_LOG_LEVEL            = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT     = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  # invoke_triage_runtime is only attached once the triage runtime exists —
  # during the bootstrap apply triage_runtime_arn is "" and IAM rejects a
  # statement with an empty Resources list.
  policy_statements = merge(
    {
      put_events = {
        effect    = "Allow"
        actions   = ["events:PutEvents"]
        resources = [var.bus_arn]
      }
      invoke_repo_helper = {
        effect    = "Allow"
        actions   = ["lambda:InvokeFunction"]
        resources = [var.repo_helper_function_arn]
      }
      read_triage_decision = {
        # Read the agent's persisted TriageDecision JSON from the
        # artifacts bucket so we can act on the ``ask`` path's questions.
        effect    = "Allow"
        actions   = ["s3:GetObject"]
        resources = ["${var.artifacts_bucket_arn}/runs/*/triage.json"]
      }
      write_synthetic_spec = {
        # Upload the 1-task synthetic spec bundle for non-spec_driven
        # workflow kinds (bug_fix / upgrade / docs). The state machine's
        # synthetic-spec branch points the Implementer at this prefix.
        effect    = "Allow"
        actions   = ["s3:PutObject"]
        resources = ["${var.artifacts_bucket_arn}/specs/*"]
      }
    },
    var.triage_runtime_arn != "" ? {
      invoke_triage_runtime = {
        # The triage agent runs as an AgentCore Runtime; the dispatcher
        # invokes it synchronously and parses the returned TriageResult.
        effect  = "Allow"
        actions = ["bedrock-agentcore:InvokeAgentRuntime"]
        resources = [
          var.triage_runtime_arn,
          "${var.triage_runtime_arn}/runtime-endpoint/*",
        ]
      }
    } : {},
  )

  tags = merge(var.tags, {
    Name      = "${local.prefix}-triage-dispatcher"
    Component = "pipeline"
  })
}

module "runtime_invoker" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-runtime-invoker"
  description   = "Step Functions shim — fires invoke_agent_runtime + waits via SendTaskSuccess."
  handler       = "runtime_invoker.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 256
  # The shim returns ~immediately (read-timeout=2s for the dispatch +
  # the network call setup), so a 60s Lambda timeout is plenty even
  # under cold-start.
  timeout      = 60
  publish      = true
  tracing_mode = "Active"

  source_path = [{
    path             = "${local.source_dir}/runtime_invoker/src"
    pip_requirements = "${local.source_dir}/runtime_invoker/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    POWERTOOLS_SERVICE_NAME       = "runtime_invoker"
    POWERTOOLS_METRICS_NAMESPACE  = "ai-dlc"
    POWERTOOLS_LOG_LEVEL          = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT   = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    invoke_agent_runtime = {
      effect    = "Allow"
      actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
      resources = length(local.runtime_arns) > 0 ? concat(local.runtime_arns, [for arn in local.runtime_arns : "${arn}/runtime-endpoint/*"]) : ["*"]
    }
    states_callbacks = {
      effect = "Allow"
      # The shim only ever calls SendTaskFailure (when dispatch fails
      # before the container can accept the work). SendTaskSuccess /
      # Heartbeat come from the agent containers themselves.
      actions   = ["states:SendTaskFailure"]
      resources = ["*"]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-runtime-invoker"
    Component = "pipeline"
  })
}

module "event_projector" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-event-projector"
  description   = "EventBridge + DDB Streams → runs read-model + AgentCore Memory CreateEvent."
  handler       = "event_projector.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 30
  publish       = true
  tracing_mode  = "Active"

  source_path = [{
    path             = "${local.source_dir}/event_projector/src"
    pip_requirements = "${local.source_dir}/event_projector/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_RUNS_TABLE              = var.runs_table
    AIDLC_MEMORY_ID               = var.memory_id
    POWERTOOLS_SERVICE_NAME       = "event_projector"
    POWERTOOLS_METRICS_NAMESPACE  = "ai-dlc"
    POWERTOOLS_LOG_LEVEL          = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT   = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    runs_table = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      resources = [var.runs_table_arn]
    }
    ddb_streams = {
      effect = "Allow"
      actions = [
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:ListStreams",
      ]
      resources = [var.runs_stream_arn, var.approvals_stream_arn]
    }
    memory_create_event = {
      effect = "Allow"
      actions = [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:GetMemory",
      ]
      resources = [var.memory_arn]
    }
  }

  event_source_mapping = {
    runs_stream = {
      event_source_arn        = var.runs_stream_arn
      starting_position       = "LATEST"
      batch_size              = 10
      function_response_types = ["ReportBatchItemFailures"]
    }
    approvals_stream = {
      event_source_arn        = var.approvals_stream_arn
      starting_position       = "LATEST"
      batch_size              = 10
      function_response_types = ["ReportBatchItemFailures"]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-event-projector"
    Component = "pipeline"
  })
}

# EventBridge rule fans every platform event into the projector.

resource "aws_cloudwatch_event_rule" "all_events" {
  name           = "${local.prefix}-projector-all"
  description    = "Forward every ai-dlc platform event to the event_projector Lambda."
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source = [{ "prefix" : "ai-dlc." }]
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-projector-all"
    Component = "pipeline"
  })
}

resource "aws_cloudwatch_event_target" "projector" {
  rule           = aws_cloudwatch_event_rule.all_events.name
  event_bus_name = var.bus_name
  arn            = module.event_projector.lambda_function_arn
}

resource "aws_lambda_permission" "events_invoke_projector" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.event_projector.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.all_events.arn
}

# EventBridge rule that converts REQUEST.RECEIVED events into Step Functions
# executions of the SDLC state machine. The InputTransformer flattens the
# envelope's payload into the shape the ASL ``Receive`` state expects.

resource "aws_cloudwatch_event_rule" "request_received" {
  name           = "${local.prefix}-pipeline-request-received"
  description    = "Start a new SDLC state-machine execution for each REQUEST.RECEIVED event."
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source      = [{ "prefix" : "ai-dlc." }]
    detail-type = ["REQUEST.RECEIVED"]
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-pipeline-request-received"
    Component = "pipeline"
  })
}

resource "aws_cloudwatch_event_target" "start_sdlc" {
  rule           = aws_cloudwatch_event_rule.request_received.name
  event_bus_name = var.bus_name
  arn            = aws_sfn_state_machine.sdlc.arn
  role_arn       = aws_iam_role.events_to_sfn.arn

  input_transformer {
    input_paths = {
      run_id              = "$.detail.run_id"
      correlation_id      = "$.detail.correlation_id"
      actor_id            = "$.detail.actor_id"
      project_slug        = "$.detail.payload.project_slug"
      intent              = "$.detail.payload.intent"
      requestor_sub       = "$.detail.payload.requestor_sub"
      target_repo         = "$.detail.payload.target_repo"
      workflow_kind       = "$.detail.payload.workflow_kind"
      synthetic_spec_slug = "$.detail.payload.synthetic_spec_slug"
    }
    input_template = <<-JSON
      {
        "run_id": "<run_id>",
        "correlation_id": "<correlation_id>",
        "actor_id": "<actor_id>",
        "project_slug": "<project_slug>",
        "intent": <intent>,
        "requestor_sub": <requestor_sub>,
        "target_repo": <target_repo>,
        "workflow_kind": <workflow_kind>,
        "synthetic_spec_slug": <synthetic_spec_slug>
      }
    JSON
  }
}

# IAM role EventBridge assumes when starting the state machine.
resource "aws_iam_role" "events_to_sfn" {
  name               = "${local.prefix}-events-to-sfn"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
  description        = "Lets EventBridge call states:StartExecution on the SDLC state machine."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-events-to-sfn"
    Component = "pipeline"
  })
}

resource "aws_iam_role_policy" "events_to_sfn" {
  name   = "events-to-sfn"
  role   = aws_iam_role.events_to_sfn.id
  policy = data.aws_iam_policy_document.events_to_sfn.json
}

data "aws_iam_policy_document" "events_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "events_to_sfn" {
  statement {
    sid       = "StartSdlcExecution"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.sdlc.arn]
  }
}
