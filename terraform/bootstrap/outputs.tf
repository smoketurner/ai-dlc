output "tfstate_bucket" {
  description = "S3 bucket holding the env-level Terraform state files."
  value       = aws_s3_bucket.tfstate.id
}

output "dns_zone_name" {
  description = "Hosted zone name."
  value       = aws_route53_zone.this.name
}

output "dns_zone_id" {
  description = "Hosted zone ID — env modules read this via `data \"aws_route53_zone\"`."
  value       = aws_route53_zone.this.zone_id
}

output "dns_zone_name_servers" {
  description = "NS records to add to the parent zone (e.g., smoketurner.com) for delegation."
  value       = aws_route53_zone.this.name_servers
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
