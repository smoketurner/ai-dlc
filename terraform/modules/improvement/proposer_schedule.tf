################################################################################
# Proposer weekly schedule.
#
# EventBridge schedule fires every Monday 09:00 UTC and invokes the Proposer's
# AgentCore Runtime via `bedrock-agentcore:invokeAgentRuntime`. The runtime
# ARN is passed in by the environment composition because the agents module
# owns it.
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

