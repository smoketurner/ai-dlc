################################################################################
# EventBridge Pipe — runs-table outbox rows → state-router SQS beacon queue.
#
# The event_projector writes an OUTBOX#{event_id} row in the same
# TransactWriteItems as every state advance, so the row's existence
# proves the state moved. This pipe forwards those rows to SQS so the
# state router sees a beacon exactly when there's new work to dispatch.
#
# Atomicity comes from the DDB transaction; at-least-once delivery comes
# from DDB Streams' built-in retry. The router is idempotent under
# duplicate beacons (every dispatched action gates on a state-conditional
# pre-advance), so re-delivery is safe.
#
# IAM policy documents live in data.tf.
################################################################################

resource "aws_iam_role" "outbox_pipe" {
  name               = "${local.prefix}-outbox-pipe"
  assume_role_policy = data.aws_iam_policy_document.outbox_pipe_assume.json

  tags = merge(var.tags, {
    Name      = "${local.prefix}-outbox-pipe"
    Component = "pipeline"
  })
}

resource "aws_iam_role_policy" "outbox_pipe" {
  name   = "outbox-pipe"
  role   = aws_iam_role.outbox_pipe.id
  policy = data.aws_iam_policy_document.outbox_pipe.json
}

resource "aws_cloudwatch_log_group" "outbox_pipe" {
  name              = "/aws/vendedlogs/pipes/${local.prefix}-outbox"
  retention_in_days = var.lambda_log_retention_days

  tags = merge(var.tags, {
    Name      = "${local.prefix}-outbox-pipe"
    Component = "pipeline"
  })
}

resource "aws_pipes_pipe" "outbox" {
  name     = "${local.prefix}-outbox"
  role_arn = aws_iam_role.outbox_pipe.arn
  source   = var.runs_stream_arn
  target   = var.beacon_queue_arn

  source_parameters {
    dynamodb_stream_parameters {
      starting_position = "LATEST"
      batch_size        = 10
    }

    # Forward only INSERTs on OUTBOX# rows. Other rows in the runs table
    # (STATE, TASK#, EVENT#) flow through the same stream but aren't the
    # pipe's responsibility — they're either irrelevant to the router
    # (EVENT# timeline rows) or already projected (STATE / TASK#).
    filter_criteria {
      filter {
        pattern = jsonencode({
          eventName = ["INSERT"]
          dynamodb = {
            Keys = {
              sk = {
                S = [{ prefix = "OUTBOX#" }]
              }
            }
          }
        })
      }
    }
  }

  target_parameters {
    sqs_queue_parameters {
      # Standard queue with fair-queue grouping by project_slug — same
      # MessageGroupId the entry_adapter uses on the initial
      # REQUEST.RECEIVED beacon, so noisy-neighbor metrics are reported
      # per project.
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
      log_group_arn = aws_cloudwatch_log_group.outbox_pipe.arn
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-outbox"
    Component = "pipeline"
  })
}
