"""POST /v1/runs entry-adapter Lambda.

Validates the request body, applies idempotency via Powertools'
``DynamoDBPersistenceLayer``, then delegates to :func:`common.runs.start_run`
which publishes ``REQUEST.RECEIVED`` onto the platform bus. The event_projector
writes the EVENT + SUMMARY rows on receipt and the DDB Stream → Pipe
forwards the EVENT insert to the state-router queue as the wake-up beacon.

If publishing fails the whole call raises and Powertools' idempotency
record stays in ``IN_PROGRESS`` — the next invocation re-executes
from scratch. The 202 with ``run_id``/``correlation_id`` is returned
only after the event lands.

The dashboard service publishes through the same shared helper without
going through this Lambda; this lambda exists for the API-Gateway entry
path (programmatic clients + CI integrations) where we want the IAM
permission scoped to a single function.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.runs import start_run

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
    target_repo: str | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[\w.-]+/[\w.-]+$",
    )


@idempotent_function(
    data_keyword_argument="request",
    config=idempotency_config,
    persistence_store=persistence,
)
def accept_run(*, request: dict[str, Any]) -> dict[str, Any]:
    """Mint a run, write the DDB row, emit the event, send the beacon."""
    run_id, correlation_id = start_run(
        project_slug=request["project_slug"],
        intent=request["intent"],
        requestor=request["requestor"],
        target_repo=request.get("target_repo"),
    )
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
