output "tfstate_bucket" {
  description = "S3 bucket holding the env-level Terraform state files."
  value       = aws_s3_bucket.tfstate.id
}

output "tfstate_lock_table" {
  description = "DynamoDB table used for state-locking."
  value       = aws_dynamodb_table.tfstate_locks.name
}

output "tfstate_kms_alias" {
  description = "KMS alias used to encrypt the tfstate bucket."
  value       = aws_kms_alias.tfstate.name
}

output "backend_hcl" {
  description = "Snippet to paste into envs/<env>/backend.tf."
  value = <<EOT
terraform {
  backend "s3" {
    bucket         = "${aws_s3_bucket.tfstate.id}"
    key            = "envs/<ENV>/terraform.tfstate"
    region         = "${var.region}"
    dynamodb_table = "${aws_dynamodb_table.tfstate_locks.name}"
    encrypt        = true
    kms_key_id     = "${aws_kms_alias.tfstate.name}"
  }
}
EOT
}
