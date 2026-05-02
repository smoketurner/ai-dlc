################################################################################
# Step Functions Standard state machine + supporting role + log group.
#
# The ASL template lives in asl/sdlc.asl.json.tftpl and is rendered with the
# concrete table names, agent runtime ARNs, bus name, and Lambda function
# name. Step Functions calls bedrockagentcore:invokeAgentRuntime via the
# native SDK integration — no Lambda hop.
################################################################################

resource "aws_iam_role" "states" {
  name               = "${local.prefix}-pipeline-sm"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
  description        = "Step Functions state-machine role for the SDLC pipeline."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-pipeline-sm"
    Component = "pipeline"
  })
}

resource "aws_iam_role_policy" "states_inline" {
  name   = "states-inline"
  role   = aws_iam_role.states.id
  policy = data.aws_iam_policy_document.states_inline.json
}

resource "aws_cloudwatch_log_group" "states" {
  name              = "/aws/states/${local.prefix}-sdlc"
  retention_in_days = var.lambda_log_retention_days

  tags = merge(var.tags, {
    Name      = "/aws/states/${local.prefix}-sdlc"
    Component = "pipeline"
  })
}

resource "aws_sfn_state_machine" "sdlc" {
  name     = "${local.prefix}-sdlc"
  role_arn = aws_iam_role.states.arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/asl/sdlc.asl.json.tftpl", {
    runs_table              = var.runs_table
    bus_name                = var.bus_name
    architect_runtime_arn   = local.architect_runtime_arn
    implementer_runtime_arn = local.implementer_runtime_arn
    hitl_function_name      = module.hitl_handler.lambda_function_name
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.states.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-sdlc"
    Component = "pipeline"
  })
}
