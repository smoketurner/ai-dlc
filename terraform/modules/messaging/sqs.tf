################################################################################
# SQS queues:
#   * state_router         — beacon queue. One message per active run; the
#                            state_router Lambda long-polls and dispatches
#                            whatever the run's current DDB state requires.
#                            Body is just `{"run_id": "..."}` — no state.
################################################################################

resource "aws_sqs_queue" "state_router" {
  name                       = "${local.prefix}-state-router"
  visibility_timeout_seconds = var.state_router_visibility_seconds
  message_retention_seconds  = 1209600
  # The router long-polls; the receive call returns within this many
  # seconds even when the queue is empty. Tuning is per-consumer (Lambda
  # event-source-mapping uses its own polling cadence), so the queue
  # default of 0 is fine, but we set it explicitly to document intent.
  receive_wait_time_seconds = 20

  tags = merge(var.tags, {
    Name      = "${local.prefix}-state-router"
    Component = "messaging"
  })
}
