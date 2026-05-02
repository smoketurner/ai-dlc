terraform {
  backend "s3" {
    bucket       = "terraform-state-022671037892-us-east-1-an"
    key          = "envs/dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
    profile      = "aidlc-admin"
  }
}
