################################################################################
# telemetry Lambda.
#
# Writes labeled rejection records to s3://artifacts/evals/rejections/
# — read by the proposer when synthesising research proposals.
################################################################################

module "telemetry" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-telemetry"
  description   = "Categorize SDLC rejection reasons via Haiku 4.5; persist labeled records."
  handler       = "telemetry.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60
  publish       = true
  tracing_mode  = "Active"
  layers        = [var.common_layer_arn]

  source_path = [{
    path             = "${local.source_dir}/telemetry/src"
    pip_requirements = "${local.source_dir}/telemetry/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_ARTIFACTS_BUCKET       = var.artifacts_bucket
    AIDLC_TELEMETRY_MODEL_ID     = var.telemetry_model_id
    POWERTOOLS_SERVICE_NAME      = "telemetry"
    POWERTOOLS_METRICS_NAMESPACE = "ai-dlc"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT  = "false"
  })

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    s3_evals = {
      effect    = "Allow"
      actions   = ["s3:PutObject", "s3:GetObject"]
      resources = ["${var.artifacts_bucket_arn}/evals/*"]
    }
    bedrock_invoke = {
      effect  = "Allow"
      actions = ["bedrock:InvokeModel"]
      resources = [
        "arn:${local.aws_partition}:bedrock:*::foundation-model/*",
        "arn:${local.aws_partition}:bedrock:*:${local.aws_account_id}:inference-profile/*",
      ]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-telemetry"
    Component = "improvement"
  })
}

# EventBridge rule routes every rejection event to the telemetry Lambda.

resource "aws_cloudwatch_event_rule" "rejections" {
  name           = "${local.prefix}-improvement-rejections"
  description    = "Route SDLC rejection events to the telemetry agent."
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source      = [{ "prefix" : "ai-dlc." }]
    detail-type = ["SPEC.REJECTED", "TASK.REJECTED"]
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-improvement-rejections"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_target" "telemetry" {
  rule           = aws_cloudwatch_event_rule.rejections.name
  event_bus_name = var.bus_name
  arn            = module.telemetry.lambda_function_arn
}

resource "aws_lambda_permission" "events_invoke_telemetry" {
  statement_id  = "AllowEventBridgeInvokeTelemetry"
  action        = "lambda:InvokeFunction"
  function_name = module.telemetry.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rejections.arn
}

################################################################################
# retrospector_dispatcher Lambda + EventBridge rule on terminal events.
#
# Fires the Retrospector AgentCore Runtime once per terminal event
# (SPEC.APPROVED / SPEC.REJECTED / TASK.APPROVED / TASK.REJECTED /
# RUN.CANCEL_REQUESTED). Provisioned only when the retrospector image
# tag is set (i.e., the agent's container has been pushed at least once).
################################################################################

module "retrospector_dispatcher" {
  count = var.retrospector_runtime_arn == "" ? 0 : 1

  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-retrospector-dispatcher"
  description   = "Fires the Retrospector AgentCore Runtime on every terminal event."
  handler       = "retrospector_dispatcher.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 256
  timeout       = 30
  publish       = true
  tracing_mode  = "Active"
  layers        = [var.common_layer_arn]

  source_path = [{
    path             = "${local.source_dir}/retrospector_dispatcher/src"
    pip_requirements = "${local.source_dir}/retrospector_dispatcher/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_RETROSPECTOR_RUNTIME_ARN = var.retrospector_runtime_arn
    POWERTOOLS_SERVICE_NAME        = "retrospector_dispatcher"
    POWERTOOLS_METRICS_NAMESPACE   = "ai-dlc"
    POWERTOOLS_LOG_LEVEL           = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT    = "false"
  })

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    invoke_runtime = {
      effect    = "Allow"
      actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
      resources = [var.retrospector_runtime_arn]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-retrospector-dispatcher"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_rule" "terminal_events" {
  count = var.retrospector_runtime_arn == "" ? 0 : 1

  name           = "${local.prefix}-improvement-terminal-events"
  description    = "Route every terminal SDLC event to the retrospector dispatcher."
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source = [{ "prefix" : "ai-dlc." }]
    detail-type = [
      "SPEC.APPROVED",
      "SPEC.REJECTED",
      "TASK.APPROVED",
      "TASK.REJECTED",
      "RUN.CANCEL_REQUESTED",
    ]
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-improvement-terminal-events"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_target" "retrospector_dispatcher" {
  count = var.retrospector_runtime_arn == "" ? 0 : 1

  rule           = aws_cloudwatch_event_rule.terminal_events[0].name
  event_bus_name = var.bus_name
  arn            = module.retrospector_dispatcher[0].lambda_function_arn
}

resource "aws_lambda_permission" "events_invoke_retrospector_dispatcher" {
  count = var.retrospector_runtime_arn == "" ? 0 : 1

  statement_id  = "AllowEventBridgeInvokeRetrospectorDispatcher"
  action        = "lambda:InvokeFunction"
  function_name = module.retrospector_dispatcher[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.terminal_events[0].arn
}
