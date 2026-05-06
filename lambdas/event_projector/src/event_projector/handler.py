"""Projector Lambda — fans out platform events into the read model + memory.

Two trigger sources, dispatched by event shape:

* **EventBridge rule** — every event on the platform bus. We update the run
  state row in DynamoDB (so the dashboard's SSE poller has fresh data) and
  forward the event to AgentCore Memory via ``CreateEvent`` so cross-session
  memory strategies can index it.

* **DynamoDB Streams** (runs + approvals tables) — currently a no-op
  passthrough; surfaces here so the wiring exists when a stream consumer
  is needed.

The projector is idempotent: every write uses the event_id as a sort-key
suffix or a ``ConditionExpression`` that keeps repeats from clobbering
already-applied state.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="event_projector")


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
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Fan out one EventBridge event or one DDB-Stream batch to consumers."""
    if "Records" in event:
        return handle_dynamodb_stream(event)
    if "detail" in event and "detail-type" in event:
        return handle_eventbridge(event)
    logger.warning("unknown trigger shape", extra={"keys": sorted(event.keys())})
    return {"ok": False, "error": "unknown trigger"}


def handle_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Single EventBridge invocation; ``event['detail']`` is the envelope."""
    detail = event["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    event_type = detail.get("type") or event.get("detail-type")
    run_id = detail.get("run_id")
    if run_id is None:
        return {"ok": False, "error": "missing run_id"}
    upsert_run_event(run_id=run_id, event_type=event_type, envelope=detail)
    update_run_state(run_id=run_id, event_type=event_type, envelope=detail)
    forward_to_memory(detail)
    return {"ok": True, "run_id": run_id, "type": event_type}


def handle_dynamodb_stream(event: dict[str, Any]) -> dict[str, Any]:
    """Pass-through; logs the batch size and returns success."""
    count = len(event.get("Records", []))
    logger.info("ddb stream batch", extra={"records": count})
    return {"ok": True, "records": count}


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
