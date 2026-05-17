variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for the improvement Lambdas."
  type        = number
  default     = 5
}

variable "bus_name" {
  description = "EventBridge bus name (rejection events come from here)."
  type        = string
}

variable "bus_arn" {
  description = "EventBridge bus ARN."
  type        = string
}

variable "artifacts_bucket" {
  description = "S3 bucket for labeled rejection records under evals/rejections/."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "S3 bucket ARN."
  type        = string
}

variable "runs_table" {
  description = "DDB table the dispatcher reads to derive the run's runtimeUserId."
  type        = string
}

variable "runs_table_arn" {
  description = "DDB runs table ARN — scopes the dispatcher's GetItem grant."
  type        = string
}

variable "retrospector_runtime_arn" {
  description = <<-EOT
    AgentCore Runtime ARN of the Retrospector agent. May be unknown at
    plan time when the runtime is being created in the same apply — use
    ``var.retrospector_enabled`` (known at plan time) to gate
    count/for_each, and pass the ARN here for the inline Lambda env.
  EOT
  type        = string
  default     = ""
}

variable "retrospector_enabled" {
  description = <<-EOT
    Whether the retrospector_dispatcher Lambda + EventBridge rule are
    provisioned. Driven by
    ``contains(keys(var.agent_image_tags), "retrospector")`` at the env
    level so the value is known at plan time (the retrospector runtime
    ARN itself may be unknown when the runtime is first being created,
    which would otherwise break ``count`` evaluation).
  EOT
  type        = bool
  default     = false
}

variable "common_layer_arn" {
  description = "ARN of the shared Lambda layer carrying the `common` Python package."
  type        = string
}

variable "platform_repo" {
  description = <<-EOT
    ``owner/name`` of the ai-dlc platform repo. Consolidate-mode runs
    with ``destination=platform`` open their PRs against this repo, so
    Retrospector-discovered platform-papercut lessons (validator
    false-positive patterns, missing tools, agent friction) land in
    the same place the platform's own MEMORY.md / AGENTS.md live.
  EOT
  type        = string
}

variable "consolidate_schedule" {
  description = <<-EOT
    EventBridge schedule expression for the weekly consolidate fanout.
    Defaults to Monday 09:00 UTC. Override per-env if you want a
    different cadence; setting to ``""`` disables the schedule (capture
    still fires per event, but no consolidation PRs are opened).
  EOT
  type        = string
  default     = "cron(0 9 ? * MON *)"
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
