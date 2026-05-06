"""Projector Lambda — fans out platform events into the read model + memory.

Two trigger sources, dispatched by event shape:

* **EventBridge rule** — every event on the platform bus. We update the run
  state row in DynamoDB (so the dashboard's SSE poller has fresh data) and
  forward the event to AgentCore Memory via ``CreateEvent`` so cross-session
  memory strategies can index it.

* **DynamoDB Streams** (runs + approvals tables) — currently a no-op
  passthrough; surfaces here so the wiring exists when a stream consumer
  is needed. Routed through Powertools' ``BatchProcessor`` so a single
  poison record reports only itself as a partial failure rather than
  poison-pilling the whole batch.

The projector is idempotent: every write uses the event_id as a sort-key
suffix or a ``ConditionExpression`` that keeps repeats from clobbering
already-applied state.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import (
    DynamoDBRecord,
)
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext

from common.events import UntypedEnvelope

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="event_projector")
tracer = Tracer(service="event_projector")
metrics = Metrics(namespace="ai-dlc", service="event_projector")

stream_processor = BatchProcessor(event_type=EventType.DynamoDBStreams)


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def agentcore() -> Any:
    """Process-cached AgentCore data-plane client (memory CreateEvent)."""
    return boto3.client("bedrock-agentcore")


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def memory_id() -> str:
    """AgentCore Memory resource ID."""
    return os.environ["AIDLC_MEMORY_ID"]


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(
    event: dict[str, Any],
    context: LambdaContext,
) -> dict[str, Any] | PartialItemFailureResponse:
    """Fan out one EventBridge event or one DDB-Stream batch to consumers."""
    if "Records" in event:
        return process_partial_response(
            event=event,
            record_handler=process_stream_record,
            processor=stream_processor,
            context=context,
        )
    if "detail" in event and "detail-type" in event:
        return handle_eventbridge(event)
    logger.warning("unknown trigger shape", extra={"keys": sorted(event.keys())})
    return {"ok": False, "error": "unknown trigger"}


def handle_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Single EventBridge invocation; ``event['detail']`` is the envelope."""
    try:
        # parse() is typed to allow batch shapes; EventBridgeEnvelope always
        # returns the single inner model, hence the cast.
        envelope = cast(
            "UntypedEnvelope",
            parse(
                event=normalise(event),
                model=UntypedEnvelope,
                envelope=EventBridgeEnvelope,
            ),
        )
    except ValidationError as exc:
        logger.warning("invalid event", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}
    detail = envelope.model_dump(mode="json")
    run_id = str(envelope.run_id)
    event_type = envelope.type
    upsert_run_event(run_id=run_id, event_type=event_type, envelope=detail)
    update_run_state(run_id=run_id, event_type=event_type, envelope=detail)
    forward_to_memory(detail)
    metrics.add_metric(name="EventsProjected", unit=MetricUnit.Count, value=1)
    return {"ok": True, "run_id": run_id, "type": event_type}


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode ``detail`` if EventBridge ships it as a JSON string."""
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


def process_stream_record(record: DynamoDBRecord) -> None:
    """Per-record handler for the DDB Streams branch.

    Currently a no-op pass-through; raising propagates to the BatchProcessor
    which records the record's sequence number under ``batchItemFailures``
    so DDB Streams retries only the failed record.
    """
    seq = record.dynamodb.sequence_number if record.dynamodb else None
    logger.debug(
        "ddb stream record",
        extra={"event_name": record.event_name, "seq": seq},
    )


@tracer.capture_method
def upsert_run_event(*, run_id: str, event_type: str | None, envelope: dict[str, Any]) -> None:
    """Append the event to the run's timeline row."""
    event_id = envelope.get("event_id", "unknown")
    ddb().put_item(
        TableName=runs_table(),
        Item={
            "pk": {"S": f"RUN#{run_id}"},
            "sk": {"S": f"EVENT#{envelope.get('timestamp', '')}#{event_id}"},
            "type": {"S": event_type or "UNKNOWN"},
            "envelope": {"S": json.dumps(envelope)},
        },
        ConditionExpression="attribute_not_exists(sk)",
    )


@tracer.capture_method
def update_run_state(*, run_id: str, event_type: str | None, envelope: dict[str, Any]) -> None:
    """Upsert the run's STATE row with current status + cumulative metrics.

    The STATE row at sk=`STATE` is what the dashboard reads. We update it
    on every event so the runs list stays current; usage counters
    (``total_token_in``/``out``, ``total_cost_usd``, ``total_duration_ms``)
    are accumulated via DynamoDB ``ADD`` whenever an agent ``*.READY``
    event carries them. ``RUN.COMPLETED`` only sets ``tasks_completed``;
    the totals are already canonical from accumulation.
    """
    payload = envelope.get("payload") or {}
    set_parts = ["#s = :status", "updated_at = :ts"]
    add_parts: list[str] = []
    names = {"#s": "status"}
    values = {
        ":status": {"S": event_type or "UNKNOWN"},
        ":ts": {"S": envelope.get("timestamp", "")},
    }
    if isinstance(payload.get("project_slug"), str) and payload["project_slug"]:
        set_parts.append("project_slug = if_not_exists(project_slug, :proj)")
        values[":proj"] = {"S": payload["project_slug"]}
    if isinstance(payload.get("spec_slug"), str) and payload["spec_slug"]:
        set_parts.append("spec_slug = :spec")
        values[":spec"] = {"S": payload["spec_slug"]}
    # REQUEST.RECEIVED for an issue-driven run carries source_issue_url.
    # Project the URL onto the gsi1 keys so the dashboard can look up
    # in-flight runs by issue (used by the issues.unassigned cancel path).
    if (
        event_type == "REQUEST.RECEIVED"
        and isinstance(payload.get("source_issue_url"), str)
        and payload["source_issue_url"]
    ):
        set_parts.append("gsi1pk = if_not_exists(gsi1pk, :issue)")
        set_parts.append("gsi1sk = if_not_exists(gsi1sk, :runref)")
        set_parts.append("source_issue_url = if_not_exists(source_issue_url, :issue_url)")
        values[":issue"] = {"S": f"ISSUE#{payload['source_issue_url']}"}
        values[":runref"] = {"S": f"RUN#{run_id}"}
        values[":issue_url"] = {"S": payload["source_issue_url"]}
    accumulate_usage(payload, add_parts=add_parts, values=values)
    if event_type == "SPEC.READY" and isinstance(payload.get("task_count"), int):
        set_parts.append("tasks_total = :tt")
        values[":tt"] = {"N": str(payload["task_count"])}
    if event_type == "RUN.COMPLETED":
        set_parts.append("tasks_completed = :tc")
        values[":tc"] = {"N": str(int(payload.get("tasks_completed", 0)))}
    expression = "SET " + ", ".join(set_parts)
    if add_parts:
        expression += " ADD " + ", ".join(add_parts)
    ddb().update_item(
        TableName=runs_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def accumulate_usage(
    payload: dict[str, Any],
    *,
    add_parts: list[str],
    values: dict[str, dict[str, str]],
) -> None:
    """Append ADD clauses for any per-event usage fields the payload carries.

    Agent ``*.READY`` payloads (SpecReady, CritiqueReady, TaskReady,
    ReviewReady, TestReportReady) inherit from
    :class:`common.events.UsagePayload`; values default to zero when an
    agent hasn't been wired yet, and we skip the ADD for zero values to
    avoid a no-op DDB write that still costs WCUs.
    """
    in_tokens = int(payload.get("token_in", 0) or 0)
    out_tokens = int(payload.get("token_out", 0) or 0)
    cost = float(payload.get("cost_usd", 0.0) or 0.0)
    duration = int(payload.get("duration_ms", 0) or 0)
    if in_tokens:
        add_parts.append("total_token_in :ti")
        values[":ti"] = {"N": str(in_tokens)}
    if out_tokens:
        add_parts.append("total_token_out :to_")
        values[":to_"] = {"N": str(out_tokens)}
    if cost:
        add_parts.append("total_cost_usd :cost")
        values[":cost"] = {"N": str(cost)}
    if duration:
        add_parts.append("total_duration_ms :dur")
        values[":dur"] = {"N": str(duration)}


@tracer.capture_method
def forward_to_memory(envelope: dict[str, Any]) -> None:
    """Emit the envelope to AgentCore Memory as a CreateEvent."""
    actor_id = envelope.get("payload", {}).get("project_slug") or envelope.get("actor_id", "system")
    session_id = envelope.get("run_id", "system")
    try:
        agentcore().create_event(
            memoryId=memory_id(),
            actorId=actor_id,
            sessionId=session_id,
            payload=[
                {
                    "blob": json.dumps(envelope).encode("utf-8"),
                    "contentType": "application/json",
                },
            ],
        )
    except Exception as exc:
        logger.warning("memory CreateEvent failed", extra={"err": repr(exc)})
        metrics.add_metric(name="MemoryWriteFailures", unit=MetricUnit.Count, value=1)
