output "tfstate_bucket" {
  description = "S3 bucket holding the env-level Terraform state files."
  value       = aws_s3_bucket.tfstate.id
}

output "backend_hcl" {
  description = "Snippet to paste into envs/<env>/backend.tf."
  value       = <<EOT
terraform {
  backend "s3" {
    bucket       = "${aws_s3_bucket.tfstate.id}"
    key          = "envs/<ENV>/terraform.tfstate"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  }
}
EOT
}
