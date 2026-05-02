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
  description = "CloudWatch Logs retention for the platform Lambdas."
  type        = number
  default     = 30
}

variable "bus_name" {
  description = "EventBridge bus name."
  type        = string
}

variable "bus_arn" {
  description = "EventBridge bus ARN."
  type        = string
}

variable "runs_table" {
  description = "DynamoDB runs-table name."
  type        = string
}

variable "runs_table_arn" {
  description = "DynamoDB runs-table ARN."
  type        = string
}

variable "runs_stream_arn" {
  description = "DynamoDB runs-table stream ARN."
  type        = string
}

variable "approvals_table" {
  description = "DynamoDB approvals-table name."
  type        = string
}

variable "approvals_table_arn" {
  description = "DynamoDB approvals-table ARN."
  type        = string
}

variable "approvals_stream_arn" {
  description = "DynamoDB approvals-table stream ARN."
  type        = string
}

variable "idempotency_table" {
  description = "DynamoDB idempotency-keys table name."
  type        = string
}

variable "idempotency_table_arn" {
  description = "DynamoDB idempotency-keys table ARN."
  type        = string
}

variable "memory_id" {
  description = "AgentCore Memory resource ID, for the projector's CreateEvent calls."
  type        = string
}

variable "memory_arn" {
  description = "AgentCore Memory resource ARN."
  type        = string
}

variable "agent_runtime_arns" {
  description = "Map of agent name → AgentCore Runtime ARN. Empty until images are pushed."
  type        = map(string)
  default     = {}
}

variable "cognito_user_pool_arn" {
  description = "Cognito user pool ARN — used by the API Gateway JWT authorizer."
  type        = string
}

variable "cognito_audience" {
  description = "Allowed audience values for the API Gateway JWT authorizer."
  type        = list(string)
}

variable "cognito_issuer_url" {
  description = "Cognito issuer URL — used as the JWT authorizer issuer."
  type        = string
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
