################################################################################
# DynamoDB tables: runs, idempotency_keys, approvals.
#
# All tables: PAY_PER_REQUEST, KMS-SSE with the customer-managed key, PITR on.
# Streams enabled on `runs` and `approvals` so the projector Lambda can emit
# AgentCore Memory CreateEvents and update the dashboard read model.
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

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.ddb_kms_key_arn
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

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.ddb_kms_key_arn
  }

  tags = merge(var.tags, {
    Name      = "${local.table_prefix}-idempotency-keys"
    Component = "state"
  })
}

resource "aws_dynamodb_table" "approvals" {
  name         = "${local.table_prefix}-approvals"
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

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.ddb_kms_key_arn
  }

  tags = merge(var.tags, {
    Name      = "${local.table_prefix}-approvals"
    Component = "state"
  })
}
