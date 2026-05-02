terraform {
  # S3 backend with native lockfile (no DynamoDB required).
  # Bucket and kms_key_id are supplied at init time via -backend-config from
  # the bootstrap module's outputs — see the Makefile's `init` target.
  backend "s3" {
    # bucket is a placeholder, overridden at `terraform init` time with
    # -backend-config (see terraform/Makefile's init target).
    # State is encrypted at rest with SSE-S3 (AES256).
    bucket       = "ai-dlc-tfstate-placeholder"
    key          = "envs/dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
