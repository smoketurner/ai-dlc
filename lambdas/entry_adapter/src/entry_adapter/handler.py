"""POST /v1/runs entry-adapter Lambda.

Validates the request body, applies idempotency via a DDB conditional put,
emits a ``REQUEST.RECEIVED`` event onto the platform bus, and returns 202
with the new ``run_id``.

The dashboard service can publish to the bus directly without going through
this Lambda; it exists for the API-Gateway entry path (programmatic clients
+ CI integrations) where we want the IAM permission scoped to a single
function instead of a service role with broad ``events:PutEvents``.
"""

from __future__ import annotations

import base64
import json
import os
import time
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.events import EventEnvelope, RequestReceived
from common.ids import new_correlation_id, new_event_id, new_run_id

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient

logger = Logger(service="entry_adapter")


class RunRequest(BaseModel):
    """Body of POST /v1/runs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_slug: str = Field(min_length=1, max_length=64)
    intent: str = Field(min_length=1, max_length=4096)
    requestor: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def events() -> EventBridgeClient:
    """Process-cached EventBridge client."""
    return boto3.client("events")


def bus_name() -> str:
    """Platform EventBridge bus name."""
    return os.environ["AIDLC_BUS_NAME"]


def idempotency_table() -> str:
    """DynamoDB table holding idempotency keys."""
    return os.environ["AIDLC_IDEMPOTENCY_TABLE"]


def idempotency_ttl_seconds() -> int:
    """How long an idempotency key blocks re-submission. Default 24h."""
    return int(os.environ.get("AIDLC_IDEMPOTENCY_TTL", "86400"))


def reserve_idempotency(key: str, run_id: str) -> bool:
    """Reserve an idempotency key for this run_id.

    Returns ``True`` if newly reserved, ``False`` if already reserved by a
    prior request (caller returns 409 with the existing run_id).
    """
    expires_at = int(time.time()) + idempotency_ttl_seconds()
    try:
        ddb().put_item(
            TableName=idempotency_table(),
            Item={
                "idempotency_key": {"S": key},
                "run_id": {"S": run_id},
                "expires_at": {"N": str(expires_at)},
            },
            ConditionExpression="attribute_not_exists(idempotency_key)",
        )
    except ddb().exceptions.ConditionalCheckFailedException:
        return False
    return True


def get_existing_run(key: str) -> str | None:
    """Fetch the run_id previously associated with this idempotency key."""
    resp = ddb().get_item(
        TableName=idempotency_table(),
        Key={"idempotency_key": {"S": key}},
        ProjectionExpression="run_id",
    )
    item = resp.get("Item")
    if item is None:
        return None
    return item["run_id"]["S"]


def publish(envelope: EventEnvelope[RequestReceived]) -> None:
    """Emit the REQUEST.RECEIVED event onto the platform bus."""
    events().put_events(
        Entries=[
            {
                "Source": f"ai-dlc.{envelope.actor_id}",
                "DetailType": envelope.type,
                "Detail": envelope.model_dump_json(),
                "EventBusName": bus_name(),
            },
        ],
    )


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """API Gateway proxy-integration handler."""
    body_str = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8")
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as exc:
        return response(400, {"error": "invalid_json", "detail": str(exc)})
    try:
        req = RunRequest.model_validate(body)
    except ValidationError as exc:
        return response(400, {"error": "validation_error", "detail": json.loads(exc.json())})

    run_id = new_run_id()
    if not reserve_idempotency(req.idempotency_key, run_id):
        existing = get_existing_run(req.idempotency_key) or "unknown"
        logger.info("idempotency hit", extra={"existing_run_id": existing})
        return response(409, {"error": "idempotent_replay", "run_id": existing})

    correlation_id = new_correlation_id()
    envelope = EventEnvelope[RequestReceived](
        event_id=new_event_id(),
        type="REQUEST.RECEIVED",
        run_id=run_id,
        correlation_id=correlation_id,
        actor_id=req.requestor,
        payload=RequestReceived(
            project_slug=req.project_slug,
            intent=req.intent,
            requestor=req.requestor,
        ),
    )
    publish(envelope)
    logger.info("run accepted", extra={"run_id": str(run_id), "project_slug": req.project_slug})
    return response(
        202,
        {
            "run_id": str(run_id),
            "correlation_id": str(correlation_id),
            "project_slug": req.project_slug,
        },
    )


def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
