################################################################################
# Tool surface — the Lambda functions that AgentCore Gateway exposes as MCP
# tools. Source code lives at lambdas/<name>/ in the repo. The lambda module
# zips the source + pip-installs requirements (with build_in_docker=true so
# pydantic-core's Rust extension is built for linux/arm64).
#
# Each Lambda is shared across agents — per-agent restrictions are applied at
# the gateway layer (each gateway role only gets lambda:InvokeFunction on the
# tool subset that agent is allowed to call).
################################################################################

module "tool_lambda" {
  for_each = local.tools

  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-${each.key}"
  description   = "AgentCore Gateway target Lambda — ${each.key}"
  handler       = "${each.key}.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 30
  publish       = true

  source_path = [{
    path             = "${local.source_dir}/${each.key}/src"
    pip_requirements = "${local.source_dir}/${each.key}/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = each.key == "repo_helper" ? merge(
    {
      AIDLC_ARTIFACTS_BUCKET = var.artifacts_bucket
      AIDLC_MEMORY_MD_BUCKET = var.memory_md_bucket
    },
    var.github_app_secret_name == null ? {} : {
      AIDLC_GITHUB_APP_SECRET_ARN      = data.aws_secretsmanager_secret.github_app[0].arn
      AIDLC_GITHUB_OAUTH_PROVIDER_NAME = aws_bedrockagentcore_oauth2_credential_provider.github[0].name
      AIDLC_AGENT_WORKLOAD_NAME        = aws_bedrockagentcore_workload_identity.platform[0].name
    },
    ) : {
    AIDLC_ARTIFACTS_BUCKET = var.artifacts_bucket
    AIDLC_MEMORY_MD_BUCKET = var.memory_md_bucket
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = each.key == "artifact_tool" || (each.key == "repo_helper" && var.github_app_secret_name != null)
  policy_statements = each.key == "artifact_tool" ? {
    s3_artifacts = {
      effect = "Allow"
      actions = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
      ]
      resources = [
        var.artifacts_bucket_arn,
        "${var.artifacts_bucket_arn}/*",
        var.memory_md_bucket_arn,
        "${var.memory_md_bucket_arn}/*",
      ]
    }
    } : (each.key == "repo_helper" && var.github_app_secret_name != null ? {
      read_app_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue"]
        resources = [data.aws_secretsmanager_secret.github_app[0].arn]
      }
      # AgentCore Identity uses Forward-Access Session to read its own
      # internal credential-vault secret on behalf of the caller, so the
      # repo_helper role must hold GetSecretValue on the service-managed
      # ``bedrock-agentcore-identity!default/*`` secret pattern.
      read_agentcore_identity_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue"]
        resources = ["arn:aws:secretsmanager:*:*:secret:bedrock-agentcore-identity!default/*"]
      }
      agentcore_user_obo = {
        effect = "Allow"
        actions = [
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetResourceOauth2Token",
        ]
        resources = ["*"]
      }
  } : {})

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}"
    Component = "agents"
  })
}
