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
  default     = 30
}

variable "bus_name" {
  description = "EventBridge bus name (rejection events come from here)."
  type        = string
}

variable "bus_arn" {
  description = "EventBridge bus ARN."
  type        = string
}

variable "runs_table" {
  description = "DynamoDB runs table name."
  type        = string
}

variable "runs_table_arn" {
  description = "DynamoDB runs table ARN."
  type        = string
}

variable "runs_stream_arn" {
  description = "DynamoDB runs-table stream ARN — drives the few-shot miner."
  type        = string
}

variable "artifacts_bucket" {
  description = "S3 bucket for the labeled rejection records + few-shot examples."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "S3 bucket ARN."
  type        = string
}

variable "telemetry_model_id" {
  description = "Bedrock model id used by the telemetry agent for categorization."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "common_layer_arn" {
  description = "ARN of the shared Lambda layer carrying the `common` Python package."
  type        = string
}

variable "beacon_queue_url" {
  description = "URL of the state-router SQS beacon queue. eval_runner enqueues a beacon when it starts a run."
  type        = string
}

variable "beacon_queue_arn" {
  description = "ARN of the state-router SQS beacon queue."
  type        = string
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
