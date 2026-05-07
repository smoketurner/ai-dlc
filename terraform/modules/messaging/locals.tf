locals {
  prefix     = "${var.project}-${var.env}"
  bus_name   = "${local.prefix}-bus"
  schema_dir = "${path.module}/../../shared/schemas"

  # Versioned event types emitted by the platform. Schemas live in
  # terraform/shared/schemas/<TYPE>.json (loaded via file()).
  event_types = toset([
    "REQUEST.RECEIVED",
    "SPEC.READY",
    "SPEC.APPROVED",
    "SPEC.REJECTED",
    "TASK.READY",
    "TASK.APPROVED",
    "TASK.REJECTED",
    "TASK.ITERATION_STARTED",
    "TASK.ITERATION_COMMITTED",
    "TASK.MAX_ITERATIONS_REACHED",
    "RUN.COMPLETED",
    "RUN.FAILED",
  ])
}
