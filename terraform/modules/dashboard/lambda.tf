################################################################################
# Dashboard Lambda — container image, ARM64.
#
# Image deploys are handled out-of-band by the dashboard-build workflow:
# after pushing a new image to ECR it calls `aws lambda update-function-code`
# with the same `:latest` tag. The `aws_ecr_image` data source above
# resolves `:latest` to a digest at plan time so this resource's `image_uri`
# stays stable between the CI-driven update and the next terraform apply.
################################################################################

module "function" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name  = local.function_name
  description    = "ai-dlc dashboard (FastAPI + Mangum) behind HTTP API Gateway."
  create_package = false
  package_type   = "Image"
  image_uri      = "${var.ecr_repository_url}@${data.aws_ecr_image.dashboard.image_digest}"
  architectures  = ["arm64"]
  memory_size    = var.memory_size_mb
  timeout        = var.lambda_timeout_seconds
  publish        = true
  tracing_mode   = "Active"

  environment_variables = merge(local.common_aws_env, {
    AIDLC_ENV                         = var.env
    AIDLC_BUS_NAME                    = var.bus_name
    AIDLC_RUNS_TABLE                  = var.runs_table
    AIDLC_IDEMPOTENCY_TABLE           = var.idempotency_table
    AIDLC_BEACON_QUEUE_URL            = var.beacon_queue_url
    AIDLC_ARTIFACTS_BUCKET            = var.artifacts_bucket
    AIDLC_GITHUB_APP_SECRET_ARN       = var.github_app_secret_arn
    AIDLC_GITHUB_WEBHOOK_SECRET_ID    = var.github_webhook_secret_id
    AIDLC_COGNITO_USER_POOL_ID        = var.cognito_user_pool_id
    AIDLC_COGNITO_CLIENT_ID           = var.cognito_user_pool_client_id
    AIDLC_COGNITO_CLIENT_SECRET_ID    = var.cognito_client_secret_id
    AIDLC_COGNITO_DISCOVERY_URL       = var.cognito_discovery_url
    AIDLC_COGNITO_DOMAIN              = var.cognito_user_pool_domain
    AIDLC_COGNITO_LOGOUT_REDIRECT_URL = local.dashboard_url
    AIDLC_SESSION_SECRET_ID           = aws_secretsmanager_secret.session.id
    AIDLC_DASHBOARD_WORKLOAD_NAME     = var.dashboard_workload_name
    AIDLC_GITHUB_OAUTH_PROVIDER_NAME  = var.github_oauth_provider_name
    AIDLC_DASHBOARD_OAUTH_RETURN_URL  = var.dashboard_oauth_return_url
    AIDLC_GITHUB_BOT_LOGIN            = var.github_bot_login
    POWERTOOLS_SERVICE_NAME           = "dashboard"
    POWERTOOLS_METRICS_NAMESPACE      = "ai-dlc"
    POWERTOOLS_LOG_LEVEL              = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT       = "false"
  })

  cloudwatch_logs_retention_in_days = var.log_retention_days

  attach_policy_statements = true
  policy_statements = merge(
    {
      runs_table = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:UpdateItem",
        ]
        resources = [
          var.runs_table_arn,
          "${var.runs_table_arn}/index/*",
          var.idempotency_table_arn,
        ]
      }
      runs_table_delete = {
        effect = "Allow"
        actions = [
          "dynamodb:DeleteItem",
          "dynamodb:BatchWriteItem",
        ]
        resources = [var.runs_table_arn]
      }
      put_events = {
        effect    = "Allow"
        actions   = ["events:PutEvents"]
        resources = [var.bus_arn]
      }
      enqueue_beacon = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
        resources = [var.beacon_queue_arn]
      }
      read_github_app_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        resources = [var.github_app_secret_arn]
      }
      read_webhook_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        resources = [var.github_webhook_secret_arn]
      }
      read_session_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        resources = [aws_secretsmanager_secret.session.arn]
      }
      read_cognito_client_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        resources = [var.cognito_client_secret_arn]
      }
      read_artifacts = {
        effect = "Allow"
        actions = [
          "s3:GetObject",
          "s3:ListBucket",
        ]
        resources = [
          var.artifacts_bucket_arn,
          "${var.artifacts_bucket_arn}/*",
        ]
      }
    },
    var.dashboard_workload_name == "" ? {} : {
      agentcore_user_obo = {
        effect = "Allow"
        actions = [
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
          "bedrock-agentcore:GetResourceOauth2Token",
          "bedrock-agentcore:CompleteResourceTokenAuth",
        ]
        resources = ["*"]
      }
      read_agentcore_identity_secret = {
        effect    = "Allow"
        actions   = ["secretsmanager:GetSecretValue"]
        resources = ["arn:${local.aws_partition}:secretsmanager:*:*:secret:bedrock-agentcore-identity!default/*"]
      }
    },
  )

  tags = merge(var.tags, {
    Name      = local.function_name
    Component = "dashboard"
  })
}
