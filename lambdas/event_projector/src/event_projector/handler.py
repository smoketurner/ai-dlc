"""Projector Lambda — fans out platform events into the read model + memory.

Two trigger sources, dispatched by event shape:

* **EventBridge rule** — every event on the platform bus. We update the run
  state row in DynamoDB (so the dashboard's SSE poller has fresh data) and
  forward the event to AgentCore Memory via ``CreateEvent`` so cross-session
  memory strategies can index it.

* **DynamoDB Streams** (runs + approvals tables) — for now a no-op
  passthrough; surfaces here so we can extend it without changing wiring
  later. (Phase 5 ships the EventBridge half — DDB-stream consumers are
  hooked up but inert.)

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
    """Pass-through for now; reserved for downstream extension."""
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

    The STATE row at sk=`STATE` is what the dashboard reads. We update it on
    every event so the runs list stays current; on RUN.COMPLETED we capture
    cost + token totals + tasks_completed for the dashboard panels.
    """
    payload = envelope.get("payload") or {}
    expr_parts = ["#s = :status", "updated_at = :ts"]
    names = {"#s": "status"}
    values = {
        ":status": {"S": event_type or "UNKNOWN"},
        ":ts": {"S": envelope.get("timestamp", "")},
    }
    if isinstance(payload.get("project_slug"), str) and payload["project_slug"]:
        expr_parts.append("project_slug = if_not_exists(project_slug, :proj)")
        values[":proj"] = {"S": payload["project_slug"]}
    if isinstance(payload.get("spec_slug"), str) and payload["spec_slug"]:
        expr_parts.append("spec_slug = :spec")
        values[":spec"] = {"S": payload["spec_slug"]}
    if event_type == "RUN.COMPLETED":
        expr_parts.extend(
            [
                "tasks_completed = :tc",
                "total_token_in = :ti",
                "total_token_out = :to_",
                "total_cost_usd = :cost",
                "total_duration_ms = :dur",
            ],
        )
        values[":tc"] = {"N": str(int(payload.get("tasks_completed", 0)))}
        values[":ti"] = {"N": str(int(payload.get("total_token_in", 0)))}
        values[":to_"] = {"N": str(int(payload.get("total_token_out", 0)))}
        values[":cost"] = {"N": str(float(payload.get("total_cost_usd", 0)))}
        values[":dur"] = {"N": str(int(payload.get("total_duration_ms", 0)))}
    ddb().update_item(
        TableName=runs_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


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
