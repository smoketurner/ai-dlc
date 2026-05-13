################################################################################
# DynamoDB tables: runs, idempotency_keys.
#
# All tables: PAY_PER_REQUEST, AWS-owned key SSE (default), PITR on.
# Streams enabled on `runs` so the projector Lambda can emit AgentCore
# Memory CreateEvents and update the dashboard read model.
################################################################################

resource "aws_dynamodb_table" "runs" {
  name         = "${local.table_prefix}-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "S"
  }
  attribute {
    name = "pr_url"
    type = "S"
  }

  # gsi1: ISSUE → RUN lookup. Populated by event_projector on
  # REQUEST.RECEIVED for issue-driven runs.
  global_secondary_index {
    name = "gsi1"
    key_schema {
      attribute_name = "gsi1pk"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "gsi1sk"
      key_type       = "RANGE"
    }
    projection_type = "ALL"
  }

  # gsi_pr: PR-URL → STATE row lookup. Populated by the event_projector
  # when it applies IMPL_PR.OPENED (writes pr_url onto STATE). The
  # dashboard webhook queries this index to resolve a GitHub PR webhook
  # to the right run and emit the right business event
  # (IMPL.ITERATION_REQUESTED, CHECKS.PASSED, CHECKS.FAILED, RUN.COMPLETED, …).
  global_secondary_index {
    name = "gsi_pr"
    key_schema {
      attribute_name = "pr_url"
      key_type       = "HASH"
    }
    projection_type = "ALL"
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  # Outbox rows (sk = ``OUTBOX#{event_id}``) carry an ``expire_at``
  # attribute set ~1h after creation. Other rows (STATE, TASK#, EVENT#)
  # don't set ``expire_at`` and are therefore exempt from sweeps. The
  # pipe forwards outbox rows to SQS within seconds; TTL is a hygiene
  # backstop so the outbox doesn't accumulate.
  ttl {
    attribute_name = "expire_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name      = "${local.table_prefix}-runs"
    Component = "state"
  })
}

resource "aws_dynamodb_table" "idempotency_keys" {
  name         = "${local.table_prefix}-idempotency-keys"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "idempotency_key"

  attribute {
    name = "idempotency_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, {
    Name      = "${local.table_prefix}-idempotency-keys"
    Component = "state"
  })
}

