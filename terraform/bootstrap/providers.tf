provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        Project   = var.project
        Component = "tfstate-bootstrap"
        ManagedBy = "terraform"
      },
      var.tags,
    )
  }
}
