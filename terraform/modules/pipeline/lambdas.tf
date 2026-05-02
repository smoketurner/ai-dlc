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

  source_path = [{
    path             = "${local.source_dir}/entry_adapter/src"
    pip_requirements = "${local.source_dir}/entry_adapter/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_BUS_NAME              = var.bus_name
    AIDLC_IDEMPOTENCY_TABLE     = var.idempotency_table
    AIDLC_IDEMPOTENCY_TTL       = "86400"
    POWERTOOLS_SERVICE_NAME     = "entry_adapter"
    POWERTOOLS_LOG_LEVEL        = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days
  cloudwatch_logs_kms_key_id        = var.logs_kms_key_arn

  attach_policy_statements = true
  policy_statements = {
    idempotency_table = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:GetItem"]
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

  source_path = [{
    path             = "${local.source_dir}/hitl_handler/src"
    pip_requirements = "${local.source_dir}/hitl_handler/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_APPROVALS_TABLE       = var.approvals_table
    POWERTOOLS_SERVICE_NAME     = "hitl_handler"
    POWERTOOLS_LOG_LEVEL        = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days
  cloudwatch_logs_kms_key_id        = var.logs_kms_key_arn

  attach_policy_statements = true
  policy_statements = {
    approvals_table = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
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

  source_path = [{
    path             = "${local.source_dir}/event_projector/src"
    pip_requirements = "${local.source_dir}/event_projector/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_RUNS_TABLE            = var.runs_table
    AIDLC_MEMORY_ID             = var.memory_id
    POWERTOOLS_SERVICE_NAME     = "event_projector"
    POWERTOOLS_LOG_LEVEL        = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days
  cloudwatch_logs_kms_key_id        = var.logs_kms_key_arn

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
      event_source_arn  = var.runs_stream_arn
      starting_position = "LATEST"
      batch_size        = 10
    }
    approvals_stream = {
      event_source_arn  = var.approvals_stream_arn
      starting_position = "LATEST"
      batch_size        = 10
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
