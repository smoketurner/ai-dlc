locals {
  purposes = toset([
    "memory",       # AgentCore Memory
    "tokenvault",   # AgentCore Identity token vault
    "s3-artifacts", # S3 artifacts + memory_md buckets
    "dynamodb",     # DynamoDB tables
    "logs",         # CloudWatch Logs (& archives)
    "secrets",      # Secrets Manager (Slack webhook, GitHub OAuth, etc.)
  ])
}
