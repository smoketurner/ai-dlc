################################################################################
# AgentCore Runtime observability — vended logs delivery.
#
# Application logs (container stdout/stderr + structlog output) from each
# AgentCore Runtime are NOT delivered via the runtime IAM role's
# logs:PutLogEvents. Instead AWS publishes them through CloudWatch vended
# logs: a per-runtime delivery_source (logType=APPLICATION_LOGS) bound to a
# delivery_destination (a /aws/vendedlogs/... log group) via a delivery.
# Without this trio, no application logs land anywhere — the runtime
# returns 500s with no visibility.
#
# Per-runtime resources are created via for_each over agent_image_tags so
# delivery only stands up for runtimes that actually exist (matches the
# bootstrap pattern used elsewhere in this module).
################################################################################

resource "aws_cloudwatch_log_group" "runtime_app_logs" {
  for_each = var.agent_image_tags

  name              = "/aws/vendedlogs/bedrock-agentcore/runtime/${local.prefix}-${each.key}"
  retention_in_days = var.lambda_log_retention_days

  tags = merge(var.tags, {
    Name      = "/aws/vendedlogs/bedrock-agentcore/runtime/${local.prefix}-${each.key}"
    Component = "agents"
  })
}

resource "aws_cloudwatch_log_delivery_source" "runtime_app_logs" {
  for_each = var.agent_image_tags

  name         = "${local.prefix}-${each.key}-app-logs"
  log_type     = "APPLICATION_LOGS"
  resource_arn = aws_bedrockagentcore_agent_runtime.agent[each.key].agent_runtime_arn

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-app-logs"
    Component = "agents"
  })
}

resource "aws_cloudwatch_log_delivery_destination" "runtime_app_logs" {
  for_each = var.agent_image_tags

  name          = "${local.prefix}-${each.key}-app-logs"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.runtime_app_logs[each.key].arn
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-app-logs"
    Component = "agents"
  })
}

resource "aws_cloudwatch_log_delivery" "runtime_app_logs" {
  for_each = var.agent_image_tags

  delivery_source_name     = aws_cloudwatch_log_delivery_source.runtime_app_logs[each.key].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.runtime_app_logs[each.key].arn

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-app-logs"
    Component = "agents"
  })
}

# A second delivery for ``TRACES`` log type. APPLICATION_LOGS only carries
# AgentCore service-side request/response envelopes; the container's
# stdout/stderr (including Python tracebacks) flows through the OTEL pipeline
# as TRACES. Without this, ``aws/spans`` stays empty and ``response_payload``
# is null with no insight into why the agent is failing inside the runtime.
#
# TRACES requires an X-Ray destination (``delivery_destination_type=XRAY``);
# CloudWatch Logs is rejected by service validation. Account-level
# Transaction Search must also be enabled (``aws xray
# update-trace-segment-destination --destination CloudWatchLogs``) so X-Ray
# fans the spans into the ``aws/spans`` log group.

resource "aws_cloudwatch_log_delivery_source" "runtime_traces" {
  for_each = var.agent_image_tags

  name         = "${local.prefix}-${each.key}-traces"
  log_type     = "TRACES"
  resource_arn = aws_bedrockagentcore_agent_runtime.agent[each.key].agent_runtime_arn

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-traces"
    Component = "agents"
  })
}

resource "aws_cloudwatch_log_delivery_destination" "runtime_traces" {
  for_each = var.agent_image_tags

  name                      = "${local.prefix}-${each.key}-traces"
  delivery_destination_type = "XRAY"

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-traces"
    Component = "agents"
  })
}

resource "aws_cloudwatch_log_delivery" "runtime_traces" {
  for_each = var.agent_image_tags

  delivery_source_name     = aws_cloudwatch_log_delivery_source.runtime_traces[each.key].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.runtime_traces[each.key].arn

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-traces"
    Component = "agents"
  })
}
