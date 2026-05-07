################################################################################
# SQS queues:
#   * hitl_approvals       — buffer for the dashboard's webhook handler when
#                            it can't reach Step Functions immediately.
#   * hitl_approvals_dlq   — dead-letter for the above.
#   * eventbridge_dlq      — dead-letter for any EventBridge rule that fails
#                            to deliver. Wired in by consumer modules.
#   * state_router         — beacon queue. One message per active run; the
#                            state_router Lambda long-polls and dispatches
#                            whatever the run's current DDB state requires.
#                            Body is just `{"run_id": "..."}` — no state.
#   * state_router_dlq     — dead-letter for the beacon queue. Reserved for
#                            SQS-level pathology (a single beacon redelivered
#                            past max_receives means the consumer is stuck
#                            on a malformed payload).
################################################################################

resource "aws_sqs_queue" "hitl_approvals_dlq" {
  name                      = "${local.prefix}-hitl-approvals-dlq"
  message_retention_seconds = 1209600 # 14 days

  tags = merge(var.tags, {
    Name      = "${local.prefix}-hitl-approvals-dlq"
    Component = "messaging"
  })
}

resource "aws_sqs_queue" "hitl_approvals" {
  name                       = "${local.prefix}-hitl-approvals"
  visibility_timeout_seconds = var.hitl_visibility_seconds
  message_retention_seconds  = 1209600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.hitl_approvals_dlq.arn
    maxReceiveCount     = var.hitl_max_receives
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-hitl-approvals"
    Component = "messaging"
  })
}

resource "aws_sqs_queue" "eventbridge_dlq" {
  name                      = "${local.prefix}-eb-dlq"
  message_retention_seconds = 1209600

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eb-dlq"
    Component = "messaging"
  })
}

resource "aws_sqs_queue" "state_router_dlq" {
  name                      = "${local.prefix}-state-router-dlq"
  message_retention_seconds = 1209600 # 14 days

  tags = merge(var.tags, {
    Name      = "${local.prefix}-state-router-dlq"
    Component = "messaging"
  })
}

resource "aws_sqs_queue" "state_router" {
  name                       = "${local.prefix}-state-router"
  visibility_timeout_seconds = var.state_router_visibility_seconds
  message_retention_seconds  = 1209600
  # The router long-polls; the receive call returns within this many
  # seconds even when the queue is empty. Tuning is per-consumer (Lambda
  # event-source-mapping uses its own polling cadence), so the queue
  # default of 0 is fine, but we set it explicitly to document intent.
  receive_wait_time_seconds = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.state_router_dlq.arn
    # High threshold: most receives are normal "no work yet" polls, not
    # consumer failures. The router never fails on no-op — it leaves
    # the visibility timeout to expire naturally. A beacon redelivered
    # past 100 times almost certainly means a malformed body or a stuck
    # consumer, neither of which we want the run-state machine to
    # process further.
    maxReceiveCount = var.state_router_max_receives
  })

  tags = merge(var.tags, {
    Name      = "${local.prefix}-state-router"
    Component = "messaging"
  })
}
