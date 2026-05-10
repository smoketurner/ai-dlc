"""AWS clients and the low-level primitives the router calls.

Two kinds of code live here:

* Process-cached boto3 client factories.
* The two side-effecting primitives the router invokes — the
  atomic ``transactional_advance`` (state Update + outbox Put) and
  the AgentCore Runtime ``dispatch_to_runtime`` call — plus the
  ``now_iso`` timestamp helper used by both.

Everything above this layer (executors, circuit breaker) calls
through these primitives instead of touching boto3 directly, which
keeps mocking surfaces small and the dependency graph one-way.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

from common.ddb import PutBuilder, TransactWriteItemsBuilder, UpdateBuilder
from common.ids import new_event_id
from state_router.config import (
    DISPATCH_CONNECT_TIMEOUT_SECONDS,
    DISPATCH_READ_TIMEOUT_SECONDS,
    runs_table,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="state_router")
tracer = Tracer(service="state_router")


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def lambda_client() -> LambdaClient:
    """Process-cached Lambda client (for repo_helper invokes)."""
    return boto3.client("lambda")


@cache
def s3() -> S3Client:
    """Process-cached S3 client (for synthetic-spec uploads)."""
    return boto3.client("s3")


@cache
def runtime_client() -> Any:
    """Process-cached AgentCore Runtime client.

    The agents use the AgentCore SDK's async-task pattern
    (``add_async_task`` + a daemon thread), so the entrypoint returns
    in ~100ms and the dispatch is a clean fast call. The 10s read
    timeout covers the AgentCore frontend's worst-case acknowledge
    time — anything longer is a real failure, not the
    ReadTimeoutError-as-success pattern this client used to have.

    Client-side retries stay disabled because the dispatch contract
    is at-least-once via the SQS beacon: a real failure rolls back
    state, the breaker counter increments, and the next beacon
    re-attempts.
    """
    return boto3.client(
        "bedrock-agentcore",
        region_name=os.environ["AWS_REGION"],
        config=Config(
            connect_timeout=DISPATCH_CONNECT_TIMEOUT_SECONDS,
            read_timeout=DISPATCH_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def now_iso() -> str:
    """Tz-aware UTC ISO timestamp for ``updated_at``."""
    return datetime.now(UTC).isoformat()


OUTBOX_TTL_SECONDS = 3600
"""How long an OUTBOX row lives before DDB TTL sweeps it.

Mirrors :data:`event_projector.handler.OUTBOX_TTL_SECONDS`. The pipe
forwards within seconds; the row is needed only briefly. Stream
records persist for 24h independent of TTL, so a pipe outage of up
to 24h still recovers.
"""


@tracer.capture_method
def transactional_advance(
    *,
    run_id: str,
    project_slug: str,
    target_pk: str,
    target_sk: str,
    advance_from: str,
    advance_to: str,
    extra_attrs: dict[str, str] | None = None,
    extra_increments: dict[str, int] | None = None,
) -> bool:
    """Atomically advance state and write an OUTBOX row in one transaction.

    The state Update is conditional on the previous value (race
    guard); the OUTBOX Put is unconditional but uses a fresh ULID
    sk so re-deliveries can't collide. Both items commit together —
    if the conditional check fails, neither lands. Returns ``True``
    on success, ``False`` on a conditional-check loss.

    The OUTBOX row is what the EventBridge Pipe forwards to the
    state-router beacon queue, so every state advance produces
    exactly one beacon for the next dispatch (or a Noop ack when
    advancing into a wait state).

    Run rows use the ``current_state`` attribute; task rows use
    ``status``. Picked by ``target_sk``.

    ``extra_attrs`` adds ``SET`` clauses (string values).
    ``extra_increments`` adds ``ADD`` clauses (integer deltas) — used
    by the rollback path to bump ``dispatch_failure_count`` atomically
    with the state reversal.
    """
    state_attr = "status" if target_sk.startswith("TASK#") else "current_state"
    update = (
        UpdateBuilder(
            table=runs_table(),
            key={"pk": target_pk, "sk": target_sk},
        )
        .set(state_attr, advance_to)
        .set("updated_at", now_iso())
        .condition_eq(state_attr, advance_from)
    )
    for attribute, value in (extra_attrs or {}).items():
        update.set(attribute, value)
    for attribute, delta in (extra_increments or {}).items():
        update.add(attribute, delta)
    expire_at = int(datetime.now(UTC).timestamp()) + OUTBOX_TTL_SECONDS
    outbox = PutBuilder(
        table=runs_table(),
        item={
            "pk": f"RUN#{run_id}",
            "sk": f"OUTBOX#{new_event_id()}",
            "run_id": run_id,
            "project_slug": project_slug,
            "expire_at": expire_at,
        },
    )
    return TransactWriteItemsBuilder().update(update).put(outbox).commit(ddb())


def dispatch_to_runtime(
    *,
    runtime_arn: str,
    runtime_session_id: str,
    payload: dict[str, Any],
) -> bool:
    """Invoke the AgentCore Runtime; return ``True`` when the agent acknowledged the work.

    The agents implement the AgentCore async-task pattern: the
    entrypoint validates the input, spawns a daemon thread for the
    actual work, and returns ``{"status": "dispatched", ...}`` in
    ~100ms. So a normal dispatch returns a clean 200 well inside the
    10s read timeout.

    A :class:`ReadTimeoutError` now means a real failure — AgentCore
    didn't acknowledge within 10s. A :class:`ClientError` (4xx / 5xx)
    means the runtime rejected the request or the entrypoint raised
    before kicking off the background thread. Both feed the rollback
    path: ``execute_invoke_agent`` reverses the state advance and
    bumps ``dispatch_failure_count`` so the breaker eventually trips.
    """
    try:
        runtime_client().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
            runtimeSessionId=runtime_session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
    except ReadTimeoutError:
        logger.warning(
            "dispatch read timeout — treating as failure",
            extra={"runtime_arn": runtime_arn},
        )
        return False
    except ClientError as exc:
        logger.warning("dispatch failed", extra={"runtime_arn": runtime_arn, "err": str(exc)})
        return False
    logger.info("dispatched", extra={"runtime_arn": runtime_arn})
    return True
