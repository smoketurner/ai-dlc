"""POST /v1/runs entry-adapter Lambda.

Validates the request body, applies idempotency via Powertools'
``DynamoDBPersistenceLayer``, then performs three actions in order:

1. **DynamoDB PutItem** — writes the run's STATE row at
   ``pk=RUN#{run_id}, sk=STATE`` carrying the request fields. The
   ``current_state`` attribute is intentionally absent — the
   event_projector applies the ``REQUEST.RECEIVED → received``
   transition on the event arriving back via EventBridge.
2. **EventBridge PutEvents** — emits ``REQUEST.RECEIVED`` for the
   projector to consume.
3. **SQS SendMessage** — delivers a beacon to the state-router queue
   with ``DelaySeconds=10`` so the router doesn't race the projector's
   transition write.

If any step fails the whole call raises and Powertools' idempotency
record stays in ``IN_PROGRESS`` — the next invocation re-executes from
scratch. The 202 with ``run_id``/``correlation_id`` is returned only
after all three succeed.

The dashboard service can publish to the bus directly without going through
this Lambda; it exists for the API-Gateway entry path (programmatic clients
+ CI integrations) where we want the IAM permission scoped to a single
function instead of a service role with broad ``events:PutEvents``.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
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
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_sqs.client import SQSClient

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


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def sqs() -> SQSClient:
    """Process-cached SQS client."""
    return boto3.client("sqs")


def bus_name() -> str:
    """Platform EventBridge bus name."""
    return os.environ["AIDLC_BUS_NAME"]


def runs_table() -> str:
    """DynamoDB runs-table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def beacon_queue_url() -> str:
    """SQS beacon queue URL."""
    return os.environ["AIDLC_BEACON_QUEUE_URL"]


# Time the router waits before its first look at the run, in seconds.
# Long enough that the projector has applied REQUEST.RECEIVED →
# received before the router peeks at the STATE row.
BEACON_INITIAL_DELAY_SECONDS = 10


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


@tracer.capture_method
def write_run_row(
    *,
    run_id: str,
    correlation_id: str,
    request: dict[str, Any],
) -> None:
    """Write the run's STATE row to DynamoDB.

    ``current_state`` is intentionally absent — the event_projector
    sets it on receipt of the ``REQUEST.RECEIVED`` event so we keep a
    single writer of run state. The conditional
    ``attribute_not_exists(pk)`` guard prevents clobbering an existing
    row if ``new_run_id()`` ever collides (UUID7 makes that effectively
    impossible, but we'd rather error than overwrite).
    """
    item: dict[str, dict[str, Any]] = {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": "STATE"},
        "run_id": {"S": run_id},
        "correlation_id": {"S": correlation_id},
        "project_slug": {"S": request["project_slug"]},
        "intent": {"S": request["intent"]},
        "requestor": {"S": request["requestor"]},
        "actor_id": {"S": request["requestor"]},
        "phase": {"S": "triage"},
        "created_at": {"S": now_iso()},
        "updated_at": {"S": now_iso()},
    }
    ddb().put_item(
        TableName=runs_table(),
        Item=item,
        ConditionExpression="attribute_not_exists(pk)",
    )


@tracer.capture_method
def send_beacon(run_id: str) -> None:
    """Enqueue the SQS beacon with a 10s delay.

    Delay covers the projector's REQUEST.RECEIVED → received transition
    so the router's first look at the STATE row sees an actionable
    cursor. If the projector takes longer, the router no-ops and the
    visibility timeout re-delivers naturally.
    """
    sqs().send_message(
        QueueUrl=beacon_queue_url(),
        MessageBody=json.dumps({"run_id": run_id}),
        DelaySeconds=BEACON_INITIAL_DELAY_SECONDS,
    )


def now_iso() -> str:
    """Tz-aware ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


@idempotent_function(
    data_keyword_argument="request",
    config=idempotency_config,
    persistence_store=persistence,
)
def accept_run(*, request: dict[str, Any]) -> dict[str, Any]:
    """Mint a run, write the DDB row, emit the event, send the beacon.

    Sequence is strict: DDB ``PutItem`` first (the source of truth),
    then EventBridge ``PutEvents`` (the projector picks up state from
    here), then SQS ``SendMessage`` with a 10s delay (the router's
    first look). If any step raises, the whole call raises and
    Powertools' idempotency record stays IN_PROGRESS so the retry
    re-runs cleanly with a fresh attempt.
    """
    run_id = new_run_id()
    correlation_id = new_correlation_id()
    write_run_row(
        run_id=str(run_id),
        correlation_id=str(correlation_id),
        request=request,
    )
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
    send_beacon(str(run_id))
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
