################################################################################
# Cross-cutting CloudWatch log group. Per-component log groups are created by
# their owning module (each Lambda, the ECS task definition, the Step
# Functions state machine) — this is the application-wide group used for
# structured business events emitted by anything that doesn't have a natural
# home.
################################################################################

resource "aws_cloudwatch_log_group" "app" {
  name              = local.app_log_group
  retention_in_days = var.log_retention_days

  tags = merge(var.tags, {
    Name      = local.app_log_group
    Component = "observability"
  })
}
