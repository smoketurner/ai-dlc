"""AWS clients and the AgentCore dispatch primitive.

The router only reads DynamoDB (for the event log) and invokes
AgentCore Runtime (for agent dispatches). Every other side-effecting
path is gone with the state-machine rewrite:

* No more conditional state advances → no ``transactional_advance``.
* No more state-row writes from the router → DDB is read-only here.
* No more ``repo_helper`` direct invokes from the router → agents
  call ``repo_helper`` through the AgentCore Gateway.
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

from common.trace_context import current_trace_context
from state_router.config import (
    DISPATCH_CONNECT_TIMEOUT_SECONDS,
    DISPATCH_READ_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="state_router")
tracer = Tracer(service="state_router")


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def runtime_client() -> Any:
    """Process-cached AgentCore Runtime client.

    The agents implement the async-task pattern: their entrypoint
    validates input, spawns a daemon thread, returns ``{"status":
    "dispatched"}`` in ~100ms. The 10s read timeout covers the worst
    case for the AgentCore frontend's acknowledgement; anything
    longer is a real failure (the executor emits ``RUN.FAILED``).
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
    """Tz-aware UTC ISO timestamp."""
    return datetime.now(UTC).isoformat()


def dispatch_to_runtime(
    *,
    runtime_arn: str,
    runtime_session_id: str,
    runtime_user_id: str,
    payload: dict[str, Any],
) -> bool:
    """Invoke the AgentCore Runtime; return ``True`` on acknowledgement.

    Fire-and-forget — the agent's container spawns a daemon thread
    and the frontend returns ``{"status": "dispatched"}`` synchronously.
    A :class:`ReadTimeoutError` or :class:`ClientError` is a real
    failure; the executor emits ``RUN.FAILED`` instead of wedging.
    """
    try:
        runtime_client().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
            runtimeSessionId=runtime_session_id,
            runtimeUserId=runtime_user_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload, default=str).encode("utf-8"),
            **current_trace_context(),
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
