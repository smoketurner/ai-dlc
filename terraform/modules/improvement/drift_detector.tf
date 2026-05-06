################################################################################
# drift_detector — eval-runner complement.
#
# Reads recent eval results from S3, compares trailing-7d pass rate against
# the trailing-30d baseline, persists a structured drift report, and emits
# the ``RegressionDetected`` CloudWatch metric. When a regression fires, it
# also publishes a structured message to the alerts SNS topic.
#
# Triggered by:
#   * EventBridge rule on the eval state machine's ExecutionSucceeded events
#     (so a fresh report is computed after every eval run completes), and
#   * A daily EventBridge schedule (so we still get a report when no eval
#     ran that day — keeps the metric alive for the alarm).
################################################################################

module "drift_detector" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  function_name = "${local.prefix}-drift-detector"
  description   = "Compare trailing eval pass rate against baseline; alarm on regression."
  handler       = "drift_detector.handler.handler"
  runtime       = "python3.13"
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 60
  publish       = true
  tracing_mode  = "Active"
  layers        = [var.common_layer_arn]

  source_path = [{
    path             = "${local.source_dir}/drift_detector/src"
    pip_requirements = "${local.source_dir}/drift_detector/requirements.txt"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"

  environment_variables = {
    AIDLC_ARTIFACTS_BUCKET       = var.artifacts_bucket
    AIDLC_ALERTS_TOPIC_ARN       = var.alerts_topic_arn
    POWERTOOLS_SERVICE_NAME      = "drift_detector"
    POWERTOOLS_METRICS_NAMESPACE = "ai-dlc"
    POWERTOOLS_LOG_LEVEL         = "INFO"
    POWERTOOLS_LOGGER_LOG_EVENT  = "false"
  }

  cloudwatch_logs_retention_in_days = var.lambda_log_retention_days

  attach_policy_statements = true
  policy_statements = {
    s3_read_results = {
      effect    = "Allow"
      actions   = ["s3:GetObject", "s3:ListBucket"]
      resources = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/evals/*"]
    }
    s3_write_drift = {
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${var.artifacts_bucket_arn}/evals/drift/*"]
    }
    cloudwatch_metric = {
      effect    = "Allow"
      actions   = ["cloudwatch:PutMetricData"]
      resources = ["*"]
    }
    sns_publish = {
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [var.alerts_topic_arn]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-drift-detector"
    Component = "improvement"
  })
}

# Trigger after each eval state-machine execution succeeds.
resource "aws_cloudwatch_event_rule" "eval_complete" {
  name        = "${local.prefix}-eval-complete-drift"
  description = "Run drift_detector after each eval state machine execution succeeds."
  event_pattern = jsonencode({
    source      = ["aws.states"]
    detail-type = ["Step Functions Execution Status Change"]
    detail = {
      stateMachineArn = [aws_sfn_state_machine.eval_runner.arn]
      status          = ["SUCCEEDED"]
    }
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-complete-drift"
    Component = "improvement"
  })
}

resource "aws_cloudwatch_event_target" "drift_after_eval" {
  rule = aws_cloudwatch_event_rule.eval_complete.name
  arn  = module.drift_detector.lambda_function_arn
}

resource "aws_lambda_permission" "events_invoke_drift_detector" {
  statement_id  = "AllowEventBridgeInvokeDriftDetector"
  action        = "lambda:InvokeFunction"
  function_name = module.drift_detector.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.eval_complete.arn
}

# Daily floor — keeps the RegressionDetected metric alive even when no eval
# ran that day, so the alarm has fresh data to evaluate against.
resource "aws_scheduler_schedule" "drift_daily" {
  name        = "${local.prefix}-drift-daily"
  group_name  = "default"
  description = "Daily drift-detector run; keeps the regression metric fresh."

  schedule_expression          = "cron(15 7 * * ? *)" # 07:15 UTC daily
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = module.drift_detector.lambda_function_arn
    role_arn = aws_iam_role.scheduler_drift.arn
  }
}

resource "aws_iam_role" "scheduler_drift" {
  name = "${local.prefix}-scheduler-drift"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_drift_invoke" {
  role = aws_iam_role.scheduler_drift.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = module.drift_detector.lambda_function_arn
    }]
  })
}

# Alarm on the metric the Lambda emits.
resource "aws_cloudwatch_metric_alarm" "regression" {
  alarm_name          = "${local.prefix}-eval-regression"
  alarm_description   = "Eval suite trailing-week pass rate dropped > 15% vs baseline."
  namespace           = "AIDLC/Evals"
  metric_name         = "RegressionDetected"
  statistic           = "Maximum"
  period              = 86400 # 1 day
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  ok_actions          = [var.alerts_topic_arn]

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-regression"
    Component = "improvement"
  })
}
