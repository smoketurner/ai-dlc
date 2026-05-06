################################################################################
# Nightly eval-runner schedule + drift alarm.
#
# The schedule kicks the state machine at 02:00 UTC by default. The drift
# alarm watches AIDLC/Evals/PassRate (emitted by eval_runner.aggregate_results)
# and fires when the trailing-week average drops more than `eval_drift_threshold`
# below the trailing 30-day baseline.
#
# Auto-revert is intentionally not implemented — the alarm publishes to
# the alerts SNS topic, a human evaluates, and the human rolls back. The
# auto-revert idea is parked in the roadmap.
################################################################################

resource "aws_cloudwatch_event_rule" "eval_schedule" {
  name                = "${local.prefix}-eval-runner-schedule"
  description         = "Nightly trigger for the eval-runner state machine."
  schedule_expression = var.eval_schedule_expression

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-runner-schedule"
    Component = "improvement"
  })
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "scheduler_inline" {
  statement {
    sid       = "StartEvalExecution"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.eval_runner.arn]
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-eval-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  description        = "EventBridge -> eval-runner state machine."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-scheduler"
    Component = "improvement"
  })
}

resource "aws_iam_role_policy" "scheduler_inline" {
  name   = "scheduler-inline"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_inline.json
}

resource "aws_cloudwatch_event_target" "eval_runner" {
  rule      = aws_cloudwatch_event_rule.eval_schedule.name
  target_id = "EvalRunner"
  arn       = aws_sfn_state_machine.eval_runner.arn
  role_arn  = aws_iam_role.scheduler.arn

  input = jsonencode({})
}

# Drift alarm: fires when 5 of the trailing 7 daily pass rates fall below
# (1.0 - eval_drift_threshold). CloudWatch metric math can't compare two
# rolling windows of different lengths in a single alarm — all metric{}
# blocks must share a period. A proper "vs 30-day baseline" rule would
# require the eval runner to emit pre-aggregated metrics (e.g.,
# PassRateWeekly + PassRateBaseline) for the alarm to subtract.

resource "aws_cloudwatch_metric_alarm" "eval_drift" {
  alarm_name          = "${local.prefix}-eval-drift"
  alarm_description   = "Eval-suite pass rate is below the floor on most of the last 7 days."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 7
  datapoints_to_alarm = 5
  threshold           = 1.0 - var.eval_drift_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]

  namespace   = "AIDLC/Evals"
  metric_name = "PassRate"
  period      = 86400
  statistic   = "Average"

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-drift"
    Component = "improvement"
  })
}
