################################################################################
# Eval state machine — runs the case set through the live SDLC pipeline.
#
# Triggered by:
#   * EventBridge schedule (nightly), or
#   * The evals.yml GitHub Actions workflow when prompt / model / MEMORY.md
#     files change in a PR, or
#   * Manual invocation via aws CLI / dashboard (future).
#
# The state machine relies on the eval_runner Lambda for load/evaluate/record/
# aggregate ops, and the SDLC pipeline state machine for actually running each
# case. We require the SDLC state machine ARN as input — the dev composition
# wires it through.
################################################################################

variable "sdlc_state_machine_arn" {
  description = "ARN of the SDLC pipeline state machine that the eval runner invokes."
  type        = string
}

variable "alerts_topic_arn" {
  description = "SNS topic that drift-detector alarms publish to."
  type        = string
}

variable "eval_max_concurrency" {
  description = "Maximum concurrent eval cases — bounds total spend per run."
  type        = number
  default     = 3
}

variable "eval_drift_threshold" {
  description = "Tolerable drop from a perfect (1.0) pass rate. The drift alarm fires when 5 of the last 7 days have PassRate < (1.0 - this). Fraction (0.15 = alarm below 0.85)."
  type        = number
  default     = 0.15
}

variable "eval_schedule_expression" {
  description = "EventBridge schedule expression for the nightly eval run."
  type        = string
  default     = "cron(0 2 * * ? *)"
}

data "aws_iam_policy_document" "eval_states_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "eval_states_inline" {
  statement {
    sid       = "InvokeEvalRunner"
    actions   = ["lambda:InvokeFunction"]
    resources = [module.eval_runner.lambda_function_arn]
  }

  statement {
    sid     = "StartSdlcExecution"
    actions = ["states:StartExecution"]
    resources = [
      var.sdlc_state_machine_arn,
    ]
  }

  statement {
    sid = "WaitForSdlcExecution"
    actions = [
      "states:DescribeExecution",
      "states:StopExecution",
    ]
    resources = [
      "${replace(var.sdlc_state_machine_arn, ":stateMachine:", ":execution:")}*",
    ]
  }

  statement {
    sid = "WaitForChildEvents"
    actions = [
      "events:PutTargets",
      "events:PutRule",
      "events:DescribeRule",
    ]
    resources = ["arn:${local.aws_partition}:events:*:${local.aws_account_id}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule"]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "eval_states" {
  name               = "${local.prefix}-eval-runner-sm"
  assume_role_policy = data.aws_iam_policy_document.eval_states_assume.json
  description        = "Step Functions state-machine role for the eval runner."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-runner-sm"
    Component = "improvement"
  })
}

resource "aws_iam_role_policy" "eval_states_inline" {
  name   = "states-inline"
  role   = aws_iam_role.eval_states.id
  policy = data.aws_iam_policy_document.eval_states_inline.json
}

resource "aws_cloudwatch_log_group" "eval_states" {
  name              = "/aws/states/${local.prefix}-eval-runner"
  retention_in_days = var.lambda_log_retention_days

  tags = merge(var.tags, {
    Name      = "/aws/states/${local.prefix}-eval-runner"
    Component = "improvement"
  })
}

resource "aws_sfn_state_machine" "eval_runner" {
  name     = "${local.prefix}-eval-runner"
  role_arn = aws_iam_role.eval_states.arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/asl/eval.asl.json.tftpl", {
    eval_runner_function_arn = module.eval_runner.lambda_function_arn
    sdlc_state_machine_arn   = var.sdlc_state_machine_arn
    max_concurrency          = var.eval_max_concurrency
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.eval_states.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-runner"
    Component = "improvement"
  })
}
