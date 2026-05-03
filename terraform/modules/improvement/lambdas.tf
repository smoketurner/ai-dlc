################################################################################
# Phase 9a Lambdas — telemetry + few_shot_miner.
#
# Both Lambdas write labeled records to s3://artifacts/evals/... — the
# substrate the eval runner (9b) and improvement proposer (9c) read from.
################################################################################

module "telemetry" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-telemetry"
  description   = "Categorize SDLC rejection reasons via Haiku 4.5; persist labeled records."
  handler       = "telemetry.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60
  publish       = true

  source_path = [{
    path             = "${local.source_dir}/telemetry/src"
    pip_requirements = "${local.source_dir}/telemetry/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_ARTIFACTS_BUCKET      = var.artifacts_bucket
    AIDLC_RUNS_TABLE            = var.runs_table
    AIDLC_TELEMETRY_MODEL_ID    = var.telemetry_model_id
    POWERTOOLS_SERVICE_NAME     = "telemetry"
    POWERTOOLS_LOG_LEVEL        = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    s3_evals = {
      effect    = "Allow"
      actions   = ["s3:PutObject", "s3:GetObject"]
      resources = ["${var.artifacts_bucket_arn}/evals/*"]
    }
    runs_table = {
      effect    = "Allow"
      actions   = ["dynamodb:UpdateItem"]
      resources = [var.runs_table_arn]
    }
    bedrock_invoke = {
      effect  = "Allow"
      actions = ["bedrock:InvokeModel"]
      resources = [
        "arn:${local.aws_partition}:bedrock:*::foundation-model/*",
        "arn:${local.aws_partition}:bedrock:*:${local.aws_account_id}:inference-profile/*",
      ]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-telemetry"
    Component = "improvement"
  })
}

module "few_shot_miner" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-few-shot-miner"
  description   = "Mine successful runs for (intent→spec) and (task→diff) few-shot examples."
  handler       = "few_shot_miner.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60
  publish       = true

  source_path = [{
    path             = "${local.source_dir}/few_shot_miner/src"
    pip_requirements = "${local.source_dir}/few_shot_miner/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_ARTIFACTS_BUCKET      = var.artifacts_bucket
    AIDLC_RUNS_TABLE            = var.runs_table
    POWERTOOLS_SERVICE_NAME     = "few_shot_miner"
    POWERTOOLS_LOG_LEVEL        = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    s3_evals = {
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${var.artifacts_bucket_arn}/evals/*"]
    }
    runs_table_read = {
      effect    = "Allow"
      actions   = ["dynamodb:Query"]
      resources = [var.runs_table_arn]
    }
    ddb_streams = {
      effect = "Allow"
      actions = [
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:ListStreams",
      ]
      resources = [var.runs_stream_arn]
    }
  }

  event_source_mapping = {
    runs_stream = {
      event_source_arn  = var.runs_stream_arn
      starting_position = "LATEST"
      batch_size        = 10
      filter_criteria = [{
        pattern = jsonencode({
          eventName = ["MODIFY", "INSERT"]
          dynamodb = {
            NewImage = {
              sk     = { S = ["STATE"] }
              status = { S = ["RUN.COMPLETED"] }
            }
          }
        })
      }]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-few-shot-miner"
    Component = "improvement"
  })
}

# EventBridge rule routes every rejection event to the telemetry Lambda.

resource "aws_cloudwatch_event_rule" "rejections" {
  name           = "${local.prefix}-improvement-rejections"
  description    = "Route SDLC rejection events to the telemetry agent."
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source      = [{ "prefix" : "ai-dlc." }]
    detail-type = ["SPEC.REJECTED", "TASK.REJECTED"]
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-improvement-rejections"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_target" "telemetry" {
  rule           = aws_cloudwatch_event_rule.rejections.name
  event_bus_name = var.bus_name
  arn            = module.telemetry.lambda_function_arn
}

resource "aws_lambda_permission" "events_invoke_telemetry" {
  statement_id  = "AllowEventBridgeInvokeTelemetry"
  action        = "lambda:InvokeFunction"
  function_name = module.telemetry.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.rejections.arn
}
