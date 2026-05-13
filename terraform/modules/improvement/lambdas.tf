################################################################################
# retrospector_dispatcher Lambda + EventBridge rule on terminal events.
#
# Fires the Retrospector AgentCore Runtime once per terminal event
# (RUN.COMPLETED / RUN.FAILED / RUN.CANCEL_REQUESTED). Provisioned only
# when the retrospector image tag is set (i.e., the agent's container
# has been pushed at least once).
################################################################################

module "retrospector_dispatcher" {
  count = var.retrospector_enabled ? 1 : 0

  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-retrospector-dispatcher"
  description   = "Fires the Retrospector AgentCore Runtime on every terminal event."
  handler       = "retrospector_dispatcher.handler.handler"
  runtime       = "python3.14"
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
  docker_image    = "public.ecr.aws/sam/build-python3.14:latest-arm64"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_RETROSPECTOR_RUNTIME_ARN = var.retrospector_runtime_arn
    AIDLC_RUNS_TABLE               = var.runs_table
    POWERTOOLS_SERVICE_NAME        = "retrospector_dispatcher"
    POWERTOOLS_METRICS_NAMESPACE   = "ai-dlc"
    POWERTOOLS_LOG_LEVEL           = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT    = "false"
  })

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    invoke_runtime = {
      effect = "Allow"
      # InvokeAgentRuntimeForUser is required when the call passes
      # ``runtimeUserId``; see pipeline/lambdas.tf for the rationale.
      actions = [
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeAgentRuntimeForUser",
      ]
      resources = [var.retrospector_runtime_arn]
    }
    read_run_state = {
      # The dispatcher reads ``requestor`` / ``requestor_sub`` off the
      # STATE row to derive ``runtimeUserId``; without this the
      # dispatch falls back to ``system:retrospector`` and we lose the
      # human-identity thread through the retrospective.
      effect    = "Allow"
      actions   = ["dynamodb:GetItem"]
      resources = [var.runs_table_arn]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-retrospector-dispatcher"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_rule" "terminal_events" {
  count = var.retrospector_enabled ? 1 : 0

  name           = "${local.prefix}-improvement-terminal-events"
  description    = "Route every terminal SDLC event to the retrospector dispatcher."
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source = [{ "prefix" : "ai-dlc." }]
    detail-type = [
      "RUN.COMPLETED",
      "RUN.FAILED",
      "RUN.CANCEL_REQUESTED",
    ]
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-improvement-terminal-events"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_target" "retrospector_dispatcher" {
  count = var.retrospector_enabled ? 1 : 0

  rule           = aws_cloudwatch_event_rule.terminal_events[0].name
  event_bus_name = var.bus_name
  arn            = module.retrospector_dispatcher[0].lambda_function_arn
}

resource "aws_lambda_permission" "events_invoke_retrospector_dispatcher" {
  count = var.retrospector_enabled ? 1 : 0

  statement_id  = "AllowEventBridgeInvokeRetrospectorDispatcher"
  action        = "lambda:InvokeFunction"
  function_name = module.retrospector_dispatcher[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.terminal_events[0].arn
}
