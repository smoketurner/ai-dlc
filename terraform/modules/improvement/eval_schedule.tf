################################################################################
# Nightly eval-runner schedule + drift alarm.
#
# The schedule kicks the state machine at 02:00 UTC by default. The drift
# alarm watches AIDLC/Evals/PassRate (emitted by eval_runner.aggregate_results)
# and fires when the trailing-week average drops more than `eval_drift_threshold`
# below the trailing 30-day baseline.
#
# Auto-revert PR is intentionally left out of v1 — the alarm publishes to
# the existing alerts SNS topic, a human evaluates, and (for now) the human
# rolls back. Auto-revert is queued for the parking lot.
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
  description        = "EventBridge → eval-runner state machine."

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

# CloudWatch metric math + alarm: trailing 7-day average vs trailing 30-day
# baseline. Fires when 7-day < 30-day - threshold for two consecutive days.

resource "aws_cloudwatch_metric_alarm" "eval_drift" {
  alarm_name          = "${local.prefix}-eval-drift"
  alarm_description   = "Eval-suite trailing-week pass rate dropped vs the 30-day baseline."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  threshold           = -1 * var.eval_drift_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]

  metric_query {
    id          = "delta"
    expression  = "weekly - baseline"
    label       = "weekly minus 30-day baseline"
    return_data = true
  }

  metric_query {
    id = "weekly"
    metric {
      namespace   = "AIDLC/Evals"
      metric_name = "PassRate"
      period      = 86400 * 7
      stat        = "Average"
    }
  }

  metric_query {
    id = "baseline"
    metric {
      namespace   = "AIDLC/Evals"
      metric_name = "PassRate"
      period      = 86400 * 30
      stat        = "Average"
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eval-drift"
    Component = "improvement"
  })
}
