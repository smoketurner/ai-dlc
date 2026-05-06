"""POST /v1/runs entry-adapter Lambda.

Validates the request body, applies idempotency via Powertools'
``DynamoDBPersistenceLayer``, emits a ``REQUEST.RECEIVED`` event onto the
platform bus, and returns 202 with the new ``run_id``. A replay of the same
``idempotency_key`` returns the cached original response (same ``run_id``,
same ``correlation_id``, same 202) — Powertools handles the in-progress /
completed / expired states correctly.

The dashboard service can publish to the bus directly without going through
this Lambda; it exists for the API-Gateway entry path (programmatic clients
+ CI integrations) where we want the IAM permission scoped to a single
function instead of a service role with broad ``events:PutEvents``.
"""

from __future__ import annotations

import base64
import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.events import EventEnvelope, RequestReceived
from common.ids import new_correlation_id, new_event_id, new_run_id

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient

logger = Logger(service="entry_adapter")
tracer = Tracer(service="entry_adapter")
metrics = Metrics(namespace="ai-dlc", service="entry_adapter")

persistence = DynamoDBPersistenceLayer(
    table_name=os.environ["AIDLC_IDEMPOTENCY_TABLE"],
    key_attr="idempotency_key",
    expiry_attr="expires_at",
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath="idempotency_key",
    expires_after_seconds=int(os.environ.get("AIDLC_IDEMPOTENCY_TTL", "86400")),
    raise_on_no_idempotency_key=True,
)


class RunRequest(BaseModel):
    """Body of POST /v1/runs."""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_slug: str = Field(min_length=1, max_length=64)
    intent: str = Field(min_length=1, max_length=4096)
    requestor: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


@cache
def events() -> EventBridgeClient:
    """Process-cached EventBridge client."""
    return boto3.client("events")


def bus_name() -> str:
    """Platform EventBridge bus name."""
    return os.environ["AIDLC_BUS_NAME"]


@tracer.capture_method
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


@idempotent_function(
    data_keyword_argument="request",
    config=idempotency_config,
    persistence_store=persistence,
)
def accept_run(*, request: dict[str, Any]) -> dict[str, Any]:
    """Mint a run, publish the REQUEST.RECEIVED event, return the 202 body.

    The argument is the validated request as a plain ``dict`` so JMESPath can
    pluck ``idempotency_key`` for the persistence layer. ``run_id``,
    ``correlation_id``, and the EventBridge publish all live inside this
    function so a replay returns the original cached response without
    re-publishing or re-minting identifiers.
    """
    run_id = new_run_id()
    correlation_id = new_correlation_id()
    envelope = EventEnvelope[RequestReceived](
        event_id=new_event_id(),
        type="REQUEST.RECEIVED",
        run_id=run_id,
        correlation_id=correlation_id,
        actor_id=request["requestor"],
        payload=RequestReceived(
            project_slug=request["project_slug"],
            intent=request["intent"],
            requestor=request["requestor"],
        ),
    )
    publish(envelope)
    metrics.add_metric(name="RunsAccepted", unit=MetricUnit.Count, value=1)
    logger.info(
        "run accepted",
        extra={"run_id": str(run_id), "project_slug": request["project_slug"]},
    )
    return {
        "run_id": str(run_id),
        "correlation_id": str(correlation_id),
        "project_slug": request["project_slug"],
    }


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """API Gateway proxy-integration handler."""
    idempotency_config.register_lambda_context(context)
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
    accepted = accept_run(request=req.model_dump())
    return response(202, accepted)


def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response."""
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
