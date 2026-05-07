"""SQS-driven state router for the SDLC pipeline.

One beacon message per active run sits on the ``state-router`` queue.
Each receive triggers this handler:

1. Read the run's STATE row + every TASK row from DynamoDB.
2. Compute the next :data:`Action` via :func:`dispatch.decide`.
3. Execute the action — invoke an agent, emit an event, write a
   synthetic spec, etc.
4. Leave the beacon undeleted so the visibility timeout expires and the
   queue re-delivers the message; the next poll will see whatever the
   projector has advanced to in the meantime. Terminal runs delete the
   beacon directly.

The router never advances state on its own initiative for "what
happened in the world" transitions — those go through the projector
applying events. The router does, however, write the per-action
``*_running`` cursor (or other internal bookkeeping advances) using
DDB ``ConditionExpression`` so concurrent routers can't dispatch the
same agent twice.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

from common.event_emit import publish
from common.state import TERMINAL_RUN_STATES
from state_router.actions import (
    Action,
    AdvanceState,
    CompoundAction,
    EmitEvent,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
    WriteSyntheticSpec,
)
from state_router.dispatch import decide
from state_router.model import Run, parse_run

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_sqs.client import SQSClient

logger = Logger(service="state_router")
tracer = Tracer(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")

DISPATCH_READ_TIMEOUT_SECONDS = 2.0
DISPATCH_CONNECT_TIMEOUT_SECONDS = 10.0


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def sqs() -> SQSClient:
    """Process-cached SQS client."""
    return boto3.client("sqs")


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
    """Process-cached AgentCore Runtime client with short read timeout.

    Same 2s read-timeout pattern as the runtime_invoker shim — the
    container accepts the request well before the timeout fires; the
    agent will emit its completion event when done.
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


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def beacon_queue_url() -> str:
    """Beacon queue URL — the router's input."""
    return os.environ["AIDLC_BEACON_QUEUE_URL"]


def artifacts_bucket() -> str:
    """Artifacts S3 bucket — for synthetic spec uploads."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


# ---------------------------------------------------------------------------
# Lambda entry
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Process every SQS record in the batch.

    Per-record: read run, decide action, execute. We never report
    partial batch failures — the router tolerates duplicate work
    (idempotent conditional updates) and the visibility timeout takes
    care of retries.
    """
    records = event.get("Records") or []
    for record in records:
        process_record(record)
    metrics.add_metric(name="BeaconsProcessed", unit=MetricUnit.Count, value=len(records))
    return {"ok": True, "count": len(records)}


def process_record(record: dict[str, Any]) -> None:
    """Decode + dispatch one beacon."""
    try:
        body = json.loads(record.get("body") or "{}")
    except json.JSONDecodeError:
        logger.warning("malformed beacon body", extra={"messageId": record.get("messageId")})
        delete_beacon(record)
        return
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        logger.warning("beacon missing run_id", extra={"body": body})
        delete_beacon(record)
        return
    run = read_run(run_id)
    if run is None:
        logger.info("orphan beacon — no run row", extra={"run_id": run_id})
        delete_beacon(record)
        return
    if run.current_state in TERMINAL_RUN_STATES:
        logger.info(
            "terminal run, deleting beacon",
            extra={"run_id": run_id, "state": str(run.current_state)},
        )
        delete_beacon(record)
        return
    action = decide(run)
    execute(run, action)


# ---------------------------------------------------------------------------
# DynamoDB read
# ---------------------------------------------------------------------------


@tracer.capture_method
def read_run(run_id: str) -> Run | None:
    """Fetch the run's STATE row + every TASK row in one Query."""
    resp = ddb().query(
        TableName=runs_table(),
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"RUN#{run_id}"}},
    )
    items = resp.get("Items") or []
    state_item: dict[str, Any] = {}
    task_items: list[dict[str, Any]] = []
    for item in items:
        sk = item.get("sk", {}).get("S", "")
        if sk == "STATE":
            state_item = item
        elif sk.startswith("TASK#"):
            task_items.append(item)
    return parse_run(state_item, task_items)


# ---------------------------------------------------------------------------
# Beacon lifecycle
# ---------------------------------------------------------------------------


def delete_beacon(record: dict[str, Any]) -> None:
    """Delete a beacon (orphan, terminal, or malformed)."""
    receipt = record.get("receiptHandle")
    if not receipt:
        return
    try:
        sqs().delete_message(QueueUrl=beacon_queue_url(), ReceiptHandle=receipt)
    except ClientError as exc:
        logger.warning("delete beacon failed", extra={"err": str(exc)})


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------


@tracer.capture_method
def execute(run: Run, action: Action) -> None:
    """Walk the action and apply its side effect.

    Compound actions recurse; everything else dispatches by type via
    :data:`EXECUTORS`.
    """
    if isinstance(action, CompoundAction):
        for sub in action.actions:
            execute(run, sub)
        return
    executor = EXECUTORS.get(type(action))
    if executor is None:
        logger.warning("unknown action type", extra={"action": type(action).__name__})
        return
    executor(run, action)


def execute_noop(run: Run, action: Action) -> None:
    """Log-only — no DDB / network call."""
    if isinstance(action, Noop):
        logger.debug("noop", extra={"run_id": run.run_id, "reason": action.reason})


def execute_emit_event(_run: Run, action: Action) -> None:
    """Emit one envelope onto the platform bus."""
    if isinstance(action, EmitEvent):
        publish(action.envelope)


EXECUTORS: dict[type[Action], Any] = {
    Noop: execute_noop,
    InvokeAgent: lambda _run, a: execute_invoke_agent(a),
    EmitEvent: execute_emit_event,
    InvokeRepoHelper: lambda _run, a: execute_invoke_repo_helper(a),
    WriteSyntheticSpec: lambda _run, a: execute_write_synthetic_spec(a),
    AdvanceState: lambda _run, a: execute_advance_state(a),
}


def execute_invoke_agent(action: InvokeAgent) -> None:
    """Conditionally advance state, then fire the agent."""
    if not advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_to,
    ):
        logger.info(
            "lost dispatch race, skipping",
            extra={"target_sk": action.target_sk, "advance_to": action.advance_to},
        )
        return
    fire_and_forget(
        runtime_arn=action.runtime_arn,
        runtime_session_id=action.runtime_session_id,
        payload=action.payload,
    )


def execute_invoke_repo_helper(action: InvokeRepoHelper) -> None:
    """Synchronous Lambda invoke; advance state on success."""
    fn = os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME")
    if not fn:
        logger.warning("repo_helper not wired", extra={"op": action.op})
        return
    response = lambda_client().invoke(
        FunctionName=fn,
        InvocationType="RequestResponse",
        Payload=json.dumps({"input": {"op": action.op, **action.args}}).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read().decode("utf-8") or "{}")
    if not body.get("ok"):
        logger.warning("repo_helper failed", extra={"op": action.op, "body": body})
        return
    if action.target_pk and action.advance_from and action.advance_to:
        extra_attrs = build_extra_attrs(action, body)
        advance_state(
            target_pk=action.target_pk,
            target_sk=action.target_sk or "STATE",
            advance_from=action.advance_from,
            advance_to=action.advance_to,
            extra_attrs=extra_attrs,
        )


def build_extra_attrs(action: InvokeRepoHelper, body: dict[str, Any]) -> dict[str, str]:
    """Extract the PR URL from a repo_helper open-PR response, if requested."""
    if not action.record_pr_url_attr:
        return {}
    pr_url = body.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url:
        return {}
    return {action.record_pr_url_attr: pr_url}


def execute_write_synthetic_spec(action: WriteSyntheticSpec) -> None:
    """Upload the three synthetic-spec docs to S3, then advance state."""
    bucket = artifacts_bucket()
    s3().put_object(
        Bucket=bucket,
        Key=f"{action.s3_key_prefix}requirements.md",
        Body=action.requirements_md.encode("utf-8"),
        ContentType="text/markdown",
    )
    s3().put_object(
        Bucket=bucket,
        Key=f"{action.s3_key_prefix}design.md",
        Body=action.design_md.encode("utf-8"),
        ContentType="text/markdown",
    )
    s3().put_object(
        Bucket=bucket,
        Key=f"{action.s3_key_prefix}tasks.md",
        Body=action.tasks_md.encode("utf-8"),
        ContentType="text/markdown",
    )
    advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_to,
    )


def execute_advance_state(action: AdvanceState) -> None:
    """Pure conditional state advance (no other side effect)."""
    advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_to,
    )


# ---------------------------------------------------------------------------
# DDB conditional state advance + agent invoke
# ---------------------------------------------------------------------------


@tracer.capture_method
def advance_state(
    *,
    target_pk: str,
    target_sk: str,
    advance_from: str,
    advance_to: str,
    extra_attrs: dict[str, str] | None = None,
) -> bool:
    """Conditionally update ``current_state`` (or task ``status``) → next.

    The condition checks the previous value to defend against
    concurrent routers. Returns ``True`` on success, ``False`` if the
    condition failed (another router advanced state first; we no-op).

    Run rows use the ``current_state`` attribute; task rows use
    ``status``. Picked by ``target_sk``.
    """
    attr = "status" if target_sk.startswith("TASK#") else "current_state"
    set_parts = ["#a = :to", "updated_at = :ts"]
    values: dict[str, dict[str, str]] = {
        ":from": {"S": advance_from},
        ":to": {"S": advance_to},
        ":ts": {"S": now_iso()},
    }
    names = {"#a": attr}
    for i, (k, v) in enumerate(extra_attrs.items() if extra_attrs else ()):
        set_parts.append(f"#k{i} = :v{i}")
        values[f":v{i}"] = {"S": v}
        names[f"#k{i}"] = k
    expression = "SET " + ", ".join(set_parts)
    try:
        ddb().update_item(
            TableName=runs_table(),
            Key={"pk": {"S": target_pk}, "sk": {"S": target_sk}},
            UpdateExpression=expression,
            ConditionExpression="#a = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def fire_and_forget(
    *,
    runtime_arn: str,
    runtime_session_id: str,
    payload: dict[str, Any],
) -> None:
    """Invoke the AgentCore Runtime; treat read-timeout as success.

    Connection-level failures (auth, throttle, not-found) are logged
    but do not raise — the next beacon poll will retry. Persistent
    failures eventually push the beacon to DLQ via maxReceiveCount.
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
        logger.info("dispatched (read-timeout)", extra={"runtime_arn": runtime_arn})
        return
    except ClientError as exc:
        logger.warning("dispatch failed", extra={"runtime_arn": runtime_arn, "err": str(exc)})
        return
    logger.info("dispatched (sub-2s response)", extra={"runtime_arn": runtime_arn})


def now_iso() -> str:
    """Tz-aware UTC ISO timestamp for ``updated_at``."""
    return datetime.now(UTC).isoformat()
