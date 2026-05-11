################################################################################
# Platform Lambdas — entry_adapter, state_router, event_projector.
#
# Each Lambda is built from `lambdas/<name>/` via the terraform-aws-modules
# wrapper (zip + pip-install dependencies in Docker for arm64). IAM is
# scoped narrowly to the resources each function actually touches.
################################################################################

module "entry_adapter" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-entry-adapter"
  description   = "POST /v1/runs → write run row → emit REQUEST.RECEIVED → enqueue state-router beacon."
  handler       = "entry_adapter.handler.handler"
  runtime       = "python3.14"
  architectures = ["arm64"]
  memory_size   = 256
  timeout       = 10
  publish       = true
  tracing_mode  = "Active"
  layers        = [var.common_layer_arn]

  source_path = [{
    path             = "${local.source_dir}/entry_adapter/src"
    pip_requirements = "${local.source_dir}/entry_adapter/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.14:latest-arm64"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_BUS_NAME               = var.bus_name
    AIDLC_IDEMPOTENCY_TABLE      = var.idempotency_table
    AIDLC_IDEMPOTENCY_TTL        = "86400"
    AIDLC_RUNS_TABLE             = var.runs_table
    AIDLC_BEACON_QUEUE_URL       = var.beacon_queue_url
    POWERTOOLS_SERVICE_NAME      = "entry_adapter"
    POWERTOOLS_METRICS_NAMESPACE = "ai-dlc"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT  = "false"
  })

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
    runs_table_put = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem"]
      resources = [var.runs_table_arn]
    }
    put_events = {
      effect    = "Allow"
      actions   = ["events:PutEvents"]
      resources = [var.bus_arn]
    }
    enqueue_beacon = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
      resources = [var.beacon_queue_arn]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-entry-adapter"
    Component = "pipeline"
  })
}

module "state_router" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-state-router"
  description   = "SQS beacon → DDB state read → action dispatch (replaces SFN orchestration)."
  handler       = "state_router.handler.handler"
  runtime       = "python3.14"
  architectures = ["arm64"]
  memory_size   = 512
  # 60s lambda timeout matches the SQS visibility timeout. Most invokes
  # finish in <2s (DDB read + 2s read-timeout fire-and-forget), but the
  # synthetic-spec write path does three S3 PutObjects sequentially.
  timeout      = 60
  publish      = true
  tracing_mode = "Active"
  layers       = [var.common_layer_arn]

  source_path = [{
    path             = "${local.source_dir}/state_router/src"
    pip_requirements = "${local.source_dir}/state_router/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.14:latest-arm64"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_RUNS_TABLE                = var.runs_table
    AIDLC_BUS_NAME                  = var.bus_name
    AIDLC_ARTIFACTS_BUCKET          = var.artifacts_bucket
    AIDLC_REPO_HELPER_FUNCTION_NAME = var.repo_helper_function_name
    AIDLC_ARCHITECT_RUNTIME_ARN     = local.architect_runtime_arn
    AIDLC_CRITIC_RUNTIME_ARN        = local.critic_runtime_arn
    AIDLC_IMPLEMENTER_RUNTIME_ARN   = local.implementer_runtime_arn
    AIDLC_PROPOSER_RUNTIME_ARN      = local.proposer_runtime_arn
    AIDLC_REVIEWER_RUNTIME_ARN      = local.reviewer_runtime_arn
    AIDLC_TESTER_RUNTIME_ARN        = local.tester_runtime_arn
    AIDLC_TRIAGE_RUNTIME_ARN        = var.triage_runtime_arn
    POWERTOOLS_SERVICE_NAME         = "state_router"
    POWERTOOLS_METRICS_NAMESPACE    = "ai-dlc"
    POWERTOOLS_LOG_LEVEL            = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT     = "false"
  })

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = merge(
    {
      runs_table = {
        # Query reads STATE + TASK rows; UpdateItem advances state
        # conditionally; PutItem (rare) for seeding task rows after
        # spec_approved; TransactWriteItems atomically reverts a
        # failed dispatch and writes the retry OUTBOX row in one
        # commit, so the pipe always sees the rollback through.
        effect = "Allow"
        actions = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:TransactWriteItems",
        ]
        resources = [var.runs_table_arn]
      }
      beacon_queue = {
        # The Lambda event source mapping handles ReceiveMessage +
        # DeleteMessage via the function execution role. The router
        # never publishes to the queue directly — beacons originate
        # from the DDB outbox row written by the projector (happy
        # path) or by the dispatch-failure rollback (retry path).
        effect = "Allow"
        actions = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        resources = [var.beacon_queue_arn]
      }
      put_events = {
        # RUN.COMPLETED + any synthetic state-advancement events.
        effect    = "Allow"
        actions   = ["events:PutEvents"]
        resources = [var.bus_arn]
      }
      invoke_repo_helper = {
        # Open spec / task PRs, post comments.
        effect    = "Allow"
        actions   = ["lambda:InvokeFunction"]
        resources = [var.repo_helper_function_arn]
      }
      write_synthetic_spec = {
        # 1-task synthetic specs for non-spec_driven workflows
        # (bug_fix / upgrade / docs) — three .md files per spec.
        effect    = "Allow"
        actions   = ["s3:PutObject"]
        resources = ["${var.artifacts_bucket_arn}/specs/*"]
      }
    },
    length(local.state_router_runtime_arns) > 0 ? {
      invoke_agent_runtime = {
        effect  = "Allow"
        actions = ["bedrock-agentcore:InvokeAgentRuntime"]
        resources = concat(
          local.state_router_runtime_arns,
          [for arn in local.state_router_runtime_arns : "${arn}/runtime-endpoint/*"],
        )
      }
    } : {},
  )

  # SQS event source mapping. Each receive batch is at most one message
  # so a slow run can't head-of-line block other beacons; long-polling
  # via the queue's ``receive_wait_time_seconds`` keeps idle cost down.
  # ``ReportBatchItemFailures`` lets the handler keep a beacon visible
  # after dispatch by returning its messageId in ``batchItemFailures`` —
  # that's how the state machine ticks between events.
  event_source_mapping = {
    state_router_queue = {
      event_source_arn        = var.beacon_queue_arn
      batch_size              = 1
      enabled                 = true
      function_response_types = ["ReportBatchItemFailures"]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-state-router"
    Component = "pipeline"
  })
}

module "event_projector" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-event-projector"
  description   = "EventBridge → runs read-model + outbox row + AgentCore Memory CreateEvent."
  handler       = "event_projector.handler.handler"
  runtime       = "python3.14"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 30
  publish       = true
  tracing_mode  = "Active"
  layers        = [var.common_layer_arn]

  source_path = [{
    path             = "${local.source_dir}/event_projector/src"
    pip_requirements = "${local.source_dir}/event_projector/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.14:latest-arm64"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_RUNS_TABLE             = var.runs_table
    AIDLC_MEMORY_ID              = var.memory_id
    POWERTOOLS_SERVICE_NAME      = "event_projector"
    POWERTOOLS_METRICS_NAMESPACE = "ai-dlc"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT  = "false"
  })

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    runs_table = {
      # PutItem writes the EVENT row + the initial STATE upsert; UpdateItem
      # accumulates usage, applies iteration accumulators; GetItem reads
      # current_state / status off STATE and TASK rows for
      # ``apply_state_transition``'s conditional updates;
      # TransactWriteItems advances state and writes the OUTBOX row in
      # one atomic step (the outbox row is what the EventBridge Pipe
      # forwards to the state-router beacon queue).
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:TransactWriteItems",
      ]
      resources = [var.runs_table_arn]
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

