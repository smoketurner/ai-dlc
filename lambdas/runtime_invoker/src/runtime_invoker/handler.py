"""Step Functions shim Lambda for long-running AgentCore Runtime calls.

The native ``arn:aws:states:::aws-sdk:bedrockagentcore:invokeAgentRuntime``
integration uses an AWS-SDK HTTP client whose read timeout is ~60s — fine
for the Architect / Critic, fatal for the Implementer (clone + Claude Code
+ push regularly takes minutes). This Lambda is invoked instead via
``lambda:invoke.waitForTaskToken``: it embeds the SF task token into the
agent payload, fires off ``invoke_agent_runtime`` with a tiny read timeout,
and returns to SF. The runtime container does the work, then calls
``states:SendTaskSuccess`` (or ``SendTaskFailure``) with the task token
when done.

Read-timeout is treated as success: by the time the read fires, the
container has already accepted the request and is processing in
background. ConnectionError / ClientError on the dispatch path itself
(auth, throttle, runtime not found) → ``SendTaskFailure`` so the SF state
fails fast instead of waiting on a token that will never come.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions.client import SFNClient

logger = Logger(service="runtime_invoker")
tracer = Tracer(service="runtime_invoker")
metrics = Metrics(namespace="ai-dlc", service="runtime_invoker")

# Long enough to establish the connection + receive AgentCore's request
# acknowledgement, short enough that the Lambda doesn't sit idle while
# the container does its multi-minute work.
DISPATCH_READ_TIMEOUT_SECONDS = 2.0
DISPATCH_CONNECT_TIMEOUT_SECONDS = 10.0


class InvokeRequest(BaseModel):
    """Input from the SF ``lambda:invoke.waitForTaskToken`` integration."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_token: str = Field(min_length=1)
    agent_runtime_arn: str = Field(
        min_length=1, pattern=r"^arn:aws:bedrock-agentcore:.+:runtime/.+$"
    )
    runtime_session_id: str = Field(min_length=33, max_length=256)
    qualifier: str = "DEFAULT"
    agent_payload: dict[str, Any]


@cache
def runtime_client() -> Any:
    """Process-cached bedrock-agentcore data-plane client with short read timeout."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=os.environ["AWS_REGION"],
        config=Config(
            connect_timeout=DISPATCH_CONNECT_TIMEOUT_SECONDS,
            read_timeout=DISPATCH_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


@cache
def sfn_client() -> SFNClient:
    """Process-cached Step Functions client (for SendTaskFailure callbacks)."""
    return boto3.client("stepfunctions", region_name=os.environ["AWS_REGION"])


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Dispatch to the agent runtime and return immediately."""
    try:
        req = InvokeRequest.model_validate(event)
    except ValidationError as exc:
        logger.warning("invalid input", extra={"errors": json.loads(exc.json())})
        return {"ok": False, "error": "validation_error"}

    payload_with_token = dict(req.agent_payload)
    payload_with_token["task_token"] = req.task_token

    try:
        runtime_client().invoke_agent_runtime(
            agentRuntimeArn=req.agent_runtime_arn,
            qualifier=req.qualifier,
            runtimeSessionId=req.runtime_session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload_with_token).encode("utf-8"),
        )
    except ReadTimeoutError:
        # Expected on the happy path: the container received the request
        # and is processing in the background. The agent will call
        # SendTaskSuccess directly when done. Lambda returns to SF.
        logger.info(
            "dispatched (read-timeout, container processing)",
            extra={"runtime_arn": req.agent_runtime_arn, "session_id": req.runtime_session_id},
        )
        return {"ok": True, "dispatched": True}
    except ClientError as exc:
        # Hard error before the container could accept the work — auth,
        # throttle, runtime missing, etc. Fail the gate immediately so
        # SF doesn't wait forever on a token that will never come.
        return fail_task(req.task_token, exc)

    # Unusual but possible: the container completed faster than the
    # 2s read window. Treat as success — the agent has either already
    # called SendTaskSuccess (in which case our return is a no-op) or
    # returned a normal HTTP response (in which case we drop it; the
    # contract is that the agent always uses SendTaskSuccess when a
    # task_token is present).
    logger.info(
        "dispatched (sub-2s response)",
        extra={"runtime_arn": req.agent_runtime_arn, "session_id": req.runtime_session_id},
    )
    return {"ok": True, "dispatched": True}


def fail_task(task_token: str, exc: ClientError) -> dict[str, Any]:
    """Send SendTaskFailure for an irrecoverable dispatch error."""
    error_code = exc.response.get("Error", {}).get("Code", "InvokeAgentRuntimeError")
    error_message = exc.response.get("Error", {}).get("Message", str(exc))
    logger.exception(
        "dispatch failed",
        extra={"error_code": error_code, "error_message": error_message[:500]},
    )
    try:
        sfn_client().send_task_failure(
            taskToken=task_token,
            error=error_code[:256],
            cause=error_message[:32_768],
        )
    except ClientError as send_exc:
        # If we can't even report the failure, surface it on the Lambda
        # invocation itself; SF's Catch will cover it.
        logger.exception("send_task_failure failed", extra={"reason": str(send_exc)})
        raise
    return {"ok": True, "dispatched": False, "failed": True}
