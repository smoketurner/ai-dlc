################################################################################
# Proposer schedule + regression trigger.
#
# Two trigger paths:
#   * Weekly EventBridge schedule  — proposer runs every Monday 09:00 UTC.
#   * Regression detected         — drift_detector's RegressionDetected metric
#                                    fires the alarm; SNS routes to the
#                                    proposer-trigger Lambda which invokes the
#                                    Proposer with trigger_reason="regression".
#
# Both paths invoke the proposer's AgentCore Runtime via
# `bedrock-agentcore:invokeAgentRuntime`. The runtime ARN is passed in by the
# environment composition because the agents module owns it.
################################################################################

variable "proposer_runtime_arn" {
  description = "AgentCore Runtime ARN of the Proposer agent. May be unknown at plan time when the runtime is being created."
  type        = string
  default     = ""
}

variable "proposer_enabled" {
  description = <<-EOT
    Whether the Proposer schedule + trigger Lambda are provisioned. Driven
    by ``contains(keys(var.agent_image_tags), "proposer")`` at the env level
    so the value is known at plan time (the runtime ARN itself may be
    unknown when the runtime is first being created, which would otherwise
    break ``count`` evaluation).
  EOT
  type        = bool
  default     = false
}

variable "proposer_target_repo" {
  description = "GitHub repo (owner/name) the Proposer opens PRs against."
  type        = string
}

variable "proposer_project_slug" {
  description = "Project slug the Proposer reads MEMORY.md / signals for."
  type        = string
}

variable "proposer_lookback_days" {
  description = "How many days of eval / rejection / few-shot data the Proposer considers."
  type        = number
  default     = 30
}

# IAM role assumed by EventBridge Scheduler when invoking the proposer runtime.
resource "aws_iam_role" "scheduler_proposer" {
  count = var.proposer_enabled ? 1 : 0

  name = "${local.prefix}-scheduler-proposer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_proposer_invoke" {
  count = var.proposer_enabled ? 1 : 0

  role = aws_iam_role.scheduler_proposer[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "bedrock-agentcore:InvokeAgentRuntime"
      Resource = var.proposer_runtime_arn
    }]
  })
}

resource "aws_scheduler_schedule" "proposer_weekly" {
  count = var.proposer_enabled ? 1 : 0

  name        = "${local.prefix}-proposer-weekly"
  group_name  = "default"
  description = "Run the Proposer agent every Monday at 09:00 UTC."

  schedule_expression          = "cron(0 9 ? * MON *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:${local.aws_partition}:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime"
    role_arn = aws_iam_role.scheduler_proposer[0].arn
    input = jsonencode({
      AgentRuntimeArn = var.proposer_runtime_arn
      Qualifier       = "DEFAULT"
      ContentType     = "application/json"
      Accept          = "application/json"
      Payload = jsonencode({
        project_slug        = var.proposer_project_slug
        target_repo         = var.proposer_target_repo
        base_branch         = "main"
        trigger_reason      = "scheduled"
        evals_lookback_days = var.proposer_lookback_days
        run_id              = "<aws.scheduler.execution-id>"
        correlation_id      = "<aws.scheduler.execution-id>"
        actor_id            = "scheduler"
      })
    })
  }
}

# Lambda that fires the proposer when the drift_detector alarm goes off.
# A small bridge — receives the SNS alarm message, invokes the proposer
# runtime with trigger_reason="regression". Lives here (not in the
# agents module) because the trigger is a proposer concern, not a fleet concern.

module "proposer_trigger" {
  count = var.proposer_enabled ? 1 : 0

  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-proposer-trigger"
  description   = "Bridges drift_detector alarms to the Proposer runtime."
  handler       = "index.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 256
  timeout       = 30
  publish       = true

  source_path = [{
    path = "${path.module}/lambda_src/proposer_trigger"
  }]
  build_in_docker = false

  environment_variables = {
    AIDLC_PROPOSER_RUNTIME_ARN = var.proposer_runtime_arn
    AIDLC_PROJECT_SLUG         = var.proposer_project_slug
    AIDLC_TARGET_REPO          = var.proposer_target_repo
    AIDLC_LOOKBACK_DAYS        = tostring(var.proposer_lookback_days)
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    invoke_runtime = {
      effect    = "Allow"
      actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
      resources = [var.proposer_runtime_arn]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-proposer-trigger"
    Component = "improvement"
  })
}

resource "aws_sns_topic_subscription" "proposer_trigger" {
  count = var.proposer_enabled ? 1 : 0

  topic_arn = var.alerts_topic_arn
  protocol  = "lambda"
  endpoint  = module.proposer_trigger[0].lambda_function_arn
}

resource "aws_lambda_permission" "sns_invoke_proposer_trigger" {
  count = var.proposer_enabled ? 1 : 0

  statement_id  = "AllowSNSInvokeProposerTrigger"
  action        = "lambda:InvokeFunction"
  function_name = module.proposer_trigger[0].lambda_function_name
  principal     = "sns.amazonaws.com"
  source_arn    = var.alerts_topic_arn
}
