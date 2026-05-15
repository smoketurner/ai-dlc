data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Event pipe — assume-role for the EventBridge Pipes service.
data "aws_iam_policy_document" "event_pipe_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["pipes.amazonaws.com"]
    }
  }
}

# Event pipe — runtime permissions: read the runs-table DDB stream and
# send to the state-router beacon queue.
data "aws_iam_policy_document" "event_pipe" {
  statement {
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [var.runs_stream_arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [var.beacon_queue_arn]
  }
}
