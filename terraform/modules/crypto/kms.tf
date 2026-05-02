################################################################################
# Customer-managed KMS keys, one per concern. Rotation enabled, 30-day deletion
# window. Aliases follow `alias/<project>/<env>/<purpose>`.
################################################################################

resource "aws_kms_key" "this" {
  for_each = local.purposes

  description             = "${var.project} ${var.env} ${each.key}"
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true

  tags = merge(var.tags, {
    Name      = "${var.project}-${var.env}-${each.key}"
    Component = "crypto"
  })
}

resource "aws_kms_alias" "this" {
  for_each = local.purposes

  name          = "alias/${var.project}/${var.env}/${each.key}"
  target_key_id = aws_kms_key.this[each.key].key_id
}
