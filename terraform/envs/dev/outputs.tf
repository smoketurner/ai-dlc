################################################################################
# Outputs surfaced for downstream consumers (CI workflows, agents, dashboards)
# via `terraform output -json`.
################################################################################

# state ------------------------------------------------------------------------

output "artifacts_bucket" {
  description = "S3 bucket holding ADRs, code diffs, and other run artifacts."
  value       = module.state.artifacts_bucket
}

output "memory_md_bucket" {
  description = "S3 bucket holding per-project MEMORY.md snapshots."
  value       = module.state.memory_md_bucket
}

output "runs_table" {
  description = "DynamoDB table for run state."
  value       = module.state.runs_table
}

output "runs_stream_arn" {
  description = "DynamoDB stream ARN for the runs table — consumed by the projector Lambda."
  value       = module.state.runs_stream_arn
}

output "idempotency_table" {
  description = "DynamoDB table for entry-Lambda idempotency keys."
  value       = module.state.idempotency_table
}

# registry ---------------------------------------------------------------------

output "ecr_repository_urls" {
  description = "ECR repository URLs by image key (architect, implementer, dashboard)."
  value       = module.registry.repository_urls
}

# messaging --------------------------------------------------------------------

output "bus_name" {
  description = "EventBridge custom bus name."
  value       = module.messaging.bus_name
}

output "bus_arn" {
  description = "EventBridge custom bus ARN."
  value       = module.messaging.bus_arn
}

# auth -------------------------------------------------------------------------

output "cognito_user_pool_id" {
  description = "Cognito user pool ID."
  value       = module.auth.user_pool_id
}

output "cognito_issuer_url" {
  description = "Cognito issuer URL — used as the JWT discovery base for AgentCore + ALB."
  value       = module.auth.issuer_url
}

output "cognito_discovery_url" {
  description = "Cognito OpenID Connect discovery URL."
  value       = module.auth.discovery_url
}

# observability ----------------------------------------------------------------

output "alerts_topic_arn" {
  description = "SNS topic that all alarms publish to."
  value       = module.observability.alerts_topic_arn
}

# agents -----------------------------------------------------------------------

output "agentcore_memory_id" {
  description = "AgentCore Memory resource ID."
  value       = module.agents.memory_id
}

output "platform_workload_arn" {
  description = "Shared AgentCore workload identity ARN used by dashboard, agents, and repo_helper."
  value       = module.agents.platform_workload_arn
}

output "platform_workload_name" {
  description = "Shared AgentCore workload identity name."
  value       = module.agents.platform_workload_name
}

output "agent_gateway_urls" {
  description = "Map of agent name → MCP endpoint URL."
  value       = module.agents.gateway_urls
}

output "agent_tool_lambda_arns" {
  description = "Map of tool Lambda name → ARN."
  value       = module.agents.tool_lambda_arns
}

output "agent_runtime_arns" {
  description = "Map of agent name → AgentCore Runtime ARN (empty until images are pushed)."
  value       = module.agents.runtime_arns
}

# pipeline ---------------------------------------------------------------------

output "api_endpoint" {
  description = "ai-dlc HTTP API endpoint (POST /v1/runs). The GitHub webhook lands on the dashboard ALB instead."
  value       = module.pipeline.api_endpoint
}

output "platform_lambda_arns" {
  description = "Map of platform Lambda name → ARN."
  value       = module.pipeline.lambda_arns
}

# dashboard --------------------------------------------------------------------

output "dashboard_url" {
  description = "Public dashboard URL (HTTPS via the Route 53 + ACM-managed FQDN)."
  value       = module.dashboard.url
}

output "dashboard_api_endpoint" {
  description = "Raw API Gateway execution URL for the dashboard Lambda."
  value       = module.dashboard.api_endpoint
}

output "dashboard_lambda_function_name" {
  description = "Dashboard Lambda function name (used by CI to update the image)."
  value       = module.dashboard.lambda_function_name
}

output "github_webhook_secret_id" {
  description = "Secrets Manager id holding the GitHub webhook HMAC secret."
  value       = aws_secretsmanager_secret.github_webhook.name
}

# improvement -----------------------------------------------------------------

output "retrospector_dispatcher_function_arn" {
  description = "Retrospector dispatcher Lambda ARN — fans terminal events to the agent."
  value       = module.improvement.retrospector_dispatcher_function_arn
}

# ci_cd ------------------------------------------------------------------------

output "github_actions_terraform_role_arn" {
  description = "Role assumed by GitHub Actions for terraform plan/apply."
  value       = module.ci_cd.terraform_role_arn
}

output "github_actions_image_publisher_role_arn" {
  description = "Role assumed by GitHub Actions to push images to ECR."
  value       = module.ci_cd.image_publisher_role_arn
}
