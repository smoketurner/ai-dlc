data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

# Lookup the current `:latest` digest at plan time so the function is
# pinned to a specific image. After the dashboard-build workflow pushes
# a new image and updates the function via `aws lambda update-function-code`,
# the next `terraform plan` reads the same digest the workflow used and
# converges without drift.
data "aws_ecr_image" "dashboard" {
  repository_name = "${var.project}/dashboard"
  image_tag       = "latest"
}
