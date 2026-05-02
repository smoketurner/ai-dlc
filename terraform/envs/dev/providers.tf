provider "aws" {
  region  = var.region
  profile = "aidlc-admin"

  default_tags {
    tags = merge(
      {
        Project   = var.project
        Env       = var.env
        Terraform = "true"
      },
      var.tags,
    )
  }
}
