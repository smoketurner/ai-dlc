locals {
  prefix     = "${var.project}-${var.env}"
  bus_name   = "${local.prefix}-bus"
  schema_dir = "${path.module}/../../shared/schemas"

  # Versioned event types emitted by the platform. Schemas live in
  # terraform/shared/schemas/<TYPE>.json (loaded via file()). Only the
  # state-advancing core events are registered here; agent advisory
  # events (REVIEW.READY, TEST_REPORT.READY, CODE_CRITIQUE.READY,
  # REVISION.READY, EVAL.DRIFT_DETECTED, RUN.CANCEL_REQUESTED) flow
  # through the bus but aren't versioned in the schema registry.
  event_types = toset([
    "REQUEST.RECEIVED",
    "ISSUE.TRIAGED",
    "DESIGN.READY",
    "CRITIQUE.READY",
    "IMPL_PR.OPENED",
    "IMPL.ITERATION_REQUESTED",
    "CHECKS.PASSED",
    "CHECKS.FAILED",
    "RUN.COMPLETED",
    "RUN.FAILED",
  ])
}
