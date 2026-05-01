resource "aws_kms_key" "tfstate" {
  description             = "Encryption key for ${var.project} terraform state."
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "tfstate" {
  name          = local.kms_alias
  target_key_id = aws_kms_key.tfstate.key_id
}
