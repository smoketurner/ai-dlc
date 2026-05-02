################################################################################
# Outputs surfaced for downstream consumers (CI workflows, agents, dashboards)
# via `terraform output -json`.
################################################################################

# crypto -----------------------------------------------------------------------

output "kms_key_arns" {
  description = "Map of KMS purpose → key ARN."
  value       = module.crypto.key_arns
}

# network ----------------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID for the env."
  value       = module.network.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (Lambdas, Fargate, optional AgentCore VPC mode)."
  value       = module.network.private_subnet_ids
}

output "public_subnet_ids" {
  description = "Public subnet IDs (ALB)."
  value       = module.network.public_subnet_ids
}

output "lambda_security_group_id" {
  description = "SG attached to platform Lambdas in private subnets."
  value       = module.network.lambda_security_group_id
}

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

output "approvals_table" {
  description = "DynamoDB table for HITL approvals."
  value       = module.state.approvals_table
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

output "hitl_approvals_queue_url" {
  description = "Buffer queue used by the dashboard webhook handler."
  value       = module.messaging.hitl_approvals_queue_url
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

output "agent_workload_identity_arns" {
  description = "Map of agent name → workload identity ARN."
  value       = module.agents.workload_identity_arns
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
  description = "ai-dlc HTTP API endpoint (POST /v1/runs, /v1/runs/{id}/decide). The GitHub webhook lands on the dashboard ALB instead."
  value       = module.pipeline.api_endpoint
}

output "state_machine_arn" {
  description = "SDLC pipeline Step Functions state machine ARN."
  value       = module.pipeline.state_machine_arn
}

output "platform_lambda_arns" {
  description = "Map of platform Lambda name → ARN."
  value       = module.pipeline.lambda_arns
}

# dashboard --------------------------------------------------------------------

output "dashboard_alb_dns" {
  description = "Dashboard ALB DNS name."
  value       = module.dashboard.alb_dns_name
}

output "dashboard_ecs_cluster" {
  description = "Dashboard ECS cluster name."
  value       = module.dashboard.ecs_cluster_name
}

output "dashboard_ecs_service" {
  description = "Dashboard ECS service name (empty until image_tag is set)."
  value       = module.dashboard.ecs_service_name
}

output "github_webhook_secret_id" {
  description = "Secrets Manager id holding the GitHub webhook HMAC secret."
  value       = aws_secretsmanager_secret.github_webhook.name
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
