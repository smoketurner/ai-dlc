################################################################################
# EventBridge Pipe — runs-table EVENT rows → state-router SQS beacon queue.
#
# The event_projector writes one ``EVENT#{event_id}`` row per platform
# event. This pipe forwards every INSERT to the state-router so the
# router decides what to do next from the run's event history.
#
# Atomicity comes from the projector's TransactWriteItems; at-least-once
# delivery comes from DDB Streams' built-in retry. The router is
# idempotent under duplicate beacons (decide() reads the events and
# returns Noop when the matching ``*.DISPATCHED`` marker is already in
# the log), so re-delivery is safe.
#
# IAM policy documents live in data.tf.
################################################################################

resource "aws_iam_role" "event_pipe" {
  name               = "${local.prefix}-event-pipe"
  assume_role_policy = data.aws_iam_policy_document.event_pipe_assume.json

  tags = merge(var.tags, {
    Name      = "${local.prefix}-event-pipe"
    Component = "pipeline"
  })
}

resource "aws_iam_role_policy" "event_pipe" {
  name   = "event-pipe"
  role   = aws_iam_role.event_pipe.id
  policy = data.aws_iam_policy_document.event_pipe.json
}

resource "aws_cloudwatch_log_group" "event_pipe" {
  name              = "/aws/vendedlogs/pipes/${local.prefix}-events"
  retention_in_days = var.lambda_log_retention_days

  tags = merge(var.tags, {
    Name      = "${local.prefix}-event-pipe"
    Component = "pipeline"
  })
}

resource "aws_pipes_pipe" "events" {
  name     = "${local.prefix}-events"
  role_arn = aws_iam_role.event_pipe.arn
  source   = var.runs_stream_arn
  target   = var.beacon_queue_arn

  source_parameters {
    dynamodb_stream_parameters {
      starting_position = "LATEST"
      batch_size        = 10
    }

    # Forward only INSERTs on EVENT# rows. SUMMARY row updates flow
    # through the same stream but carry only accumulators — nothing the
    # router needs to react to.
    filter_criteria {
      filter {
        pattern = jsonencode({
          eventName = ["INSERT"]
          dynamodb = {
            Keys = {
              sk = {
                S = [{ prefix = "EVENT#" }]
              }
            }
          }
        })
      }
    }
  }

  target_parameters {
    sqs_queue_parameters {
      # Standard queue with fair-queue grouping by project_slug so
      # noisy-neighbor metrics are reported per project.
      message_group_id = "$.dynamodb.NewImage.project_slug.S"
    }

    # Strip the DDB envelope: the SQS body is just ``{"run_id": "..."}``,
    # the shape the state_router parses today.
    input_template = <<-EOT
      {"run_id": "<$.dynamodb.NewImage.run_id.S>"}
    EOT
  }

  log_configuration {
    level                  = "INFO"
    include_execution_data = ["ALL"]
    cloudwatch_logs_log_destination {
      log_group_arn = aws_cloudwatch_log_group.event_pipe.arn
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-events"
    Component = "pipeline"
  })
}
