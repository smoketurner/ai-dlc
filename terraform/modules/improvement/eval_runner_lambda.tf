################################################################################
# eval_runner Lambda — load_cases | evaluate_result | record_result |
# aggregate_results. Step Functions calls each op separately as it walks the
# state machine.
################################################################################

module "eval_runner" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-eval-runner"
  description   = "Eval runner — load + evaluate + record + aggregate eval-case results."
  handler       = "eval_runner.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60
  publish       = true
  tracing_mode  = "Active"

  source_path = [{
    path             = "${local.source_dir}/eval_runner/src"
    pip_requirements = "${local.source_dir}/eval_runner/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_ARTIFACTS_BUCKET       = var.artifacts_bucket
    AIDLC_RUNS_TABLE             = var.runs_table
    AIDLC_EVAL_CASES_KEY         = "evals/cases.yaml"
    POWERTOOLS_SERVICE_NAME      = "eval_runner"
    POWERTOOLS_METRICS_NAMESPACE = "ai-dlc"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT  = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    s3_evals = {
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
      resources = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/evals/*"]
    }
    runs_table_read = {
      effect    = "Allow"
      actions   = ["dynamodb:GetItem"]
      resources = [var.runs_table_arn]
    }
    cloudwatch_metrics = {
      effect    = "Allow"
      actions   = ["cloudwatch:PutMetricData"]
      resources = ["*"]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-runner"
    Component = "improvement"
  })
}
