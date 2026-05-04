output "memory_id" {
  description = "AgentCore Memory resource ID."
  value       = aws_bedrockagentcore_memory.this.id
}

output "memory_arn" {
  description = "AgentCore Memory resource ARN."
  value       = aws_bedrockagentcore_memory.this.arn
}

output "workload_identity_arns" {
  description = "Map of agent name → workload identity ARN."
  value = {
    for k, v in aws_bedrockagentcore_workload_identity.agent :
    k => v.workload_identity_arn
  }
}

output "gateway_urls" {
  description = "Map of agent name → MCP endpoint URL."
  value = {
    for k, v in aws_bedrockagentcore_gateway.agent :
    k => v.gateway_url
  }
}

output "gateway_ids" {
  description = "Map of agent name → gateway ID."
  value = {
    for k, v in aws_bedrockagentcore_gateway.agent :
    k => v.gateway_id
  }
}

output "gateway_role_arns" {
  description = "Map of agent name → gateway role ARN."
  value = {
    for k, v in aws_iam_role.gateway :
    k => v.arn
  }
}

output "tool_lambda_arns" {
  description = "Map of tool name → Lambda function ARN."
  value = {
    for k, v in module.tool_lambda :
    k => v.lambda_function_arn
  }
}

output "github_oauth_provider_arn" {
  description = "AgentCore Identity GithubOauth2 credential provider ARN — handles user-OBO auth."
  value = (
    length(aws_bedrockagentcore_oauth2_credential_provider.github) > 0
    ? aws_bedrockagentcore_oauth2_credential_provider.github[0].credential_provider_arn
    : null
  )
}

output "github_oauth_provider_name" {
  description = "AgentCore Identity GithubOauth2 credential provider name — used by the dashboard's authorize URL and the repo_helper Lambda."
  value = (
    length(aws_bedrockagentcore_oauth2_credential_provider.github) > 0
    ? aws_bedrockagentcore_oauth2_credential_provider.github[0].name
    : null
  )
}

output "github_app_secret_arn" {
  description = "Secrets Manager ARN of the GitHub App credentials (app_id + private_key) for installation-token minting."
  value = (
    length(aws_secretsmanager_secret.github_app) > 0
    ? aws_secretsmanager_secret.github_app[0].arn
    : null
  )
}

output "runtime_arns" {
  description = "Map of agent name → AgentCore Runtime ARN."
  value = {
    for k, v in aws_bedrockagentcore_agent_runtime.agent : k => v.agent_runtime_arn
  }
}

output "runtime_role_arns" {
  description = "Map of agent name → runtime IAM role ARN."
  value = {
    for k, v in aws_iam_role.runtime : k => v.arn
  }
}
