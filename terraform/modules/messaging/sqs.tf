################################################################################
# SQS queues:
#   * hitl_approvals       — buffer for the dashboard's webhook handler when
#                            it can't reach Step Functions immediately.
#   * hitl_approvals_dlq   — dead-letter for the above.
#   * eventbridge_dlq      — dead-letter for any EventBridge rule that fails
#                            to deliver. Wired in by consumer modules.
################################################################################

resource "aws_sqs_queue" "hitl_approvals_dlq" {
  name                              = "${local.prefix}-hitl-approvals-dlq"
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300
  message_retention_seconds         = 1209600 # 14 days

  tags = merge(var.tags, {
    Name      = "${local.prefix}-hitl-approvals-dlq"
    Component = "messaging"
  })
}

resource "aws_sqs_queue" "hitl_approvals" {
  name                              = "${local.prefix}-hitl-approvals"
  visibility_timeout_seconds        = var.hitl_visibility_seconds
  message_retention_seconds         = 1209600
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

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
  name                              = "${local.prefix}-eb-dlq"
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300
  message_retention_seconds         = 1209600

  tags = merge(var.tags, {
    Name      = "${local.prefix}-eb-dlq"
    Component = "messaging"
  })
}
