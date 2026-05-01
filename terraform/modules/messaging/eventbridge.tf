################################################################################
# EventBridge custom bus + archive + schema registry + DLQ.
#
# The schema registry holds versioned JSON schemas for every event type the
# platform emits. Producers (Lambdas, Step Functions task states) populate
# `detail` with an `EventEnvelope[T]` matching one of these schemas.
################################################################################

resource "aws_cloudwatch_event_bus" "this" {
  name              = local.bus_name
  kms_key_identifier = var.kms_key_arn
}

resource "aws_cloudwatch_event_archive" "this" {
  name             = "${local.prefix}-archive"
  event_source_arn = aws_cloudwatch_event_bus.this.arn
  retention_days   = var.archive_retention_days
  description      = "Replayable archive for the ${var.project} bus."
}

resource "aws_schemas_registry" "this" {
  name        = "${var.project}.events"
  description = "Versioned event schemas for the ${var.project} platform."
}

resource "aws_schemas_schema" "this" {
  for_each = local.event_types

  name          = "${var.project}.events@${each.key}"
  registry_name = aws_schemas_registry.this.name
  type          = "JSONSchemaDraft4"
  description   = "Schema for ${each.key} (v1)."
  content       = file("${local.schema_dir}/${replace(each.key, ".", "_")}.json")
}
