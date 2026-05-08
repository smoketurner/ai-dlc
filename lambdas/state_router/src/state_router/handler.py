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
from common.events import EventEnvelope, RunFailed, TaskBlocked
from common.ids import CorrelationId, RunId, new_event_id
from common.state import TERMINAL_RUN_STATES
from state_router.actions import (
    Action,
    AdvanceState,
    CompoundAction,
    EmitEvent,
    GuardedAdvance,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
    SeedTasks,
    WriteSyntheticSpec,
)
from state_router.dispatch import decide
from state_router.model import Run, Task, parse_run

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="state_router")
tracer = Tracer(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")

DISPATCH_READ_TIMEOUT_SECONDS = 10.0
DISPATCH_CONNECT_TIMEOUT_SECONDS = 10.0

MAX_DISPATCH_FAILURES = 3
"""Open the per-row circuit breaker after this many consecutive
``rollback_invoke_advance`` cycles. Each rollback increments
``dispatch_failure_count`` on the target row; a successful agent
completion (``*.READY`` event) resets it to 0 in the projector.
"""


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


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


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

    Each beacon is processed once per Lambda invocation, then either:

    * **Reported as a batch-item failure** (the default for non-terminal
      runs) — Lambda's SQS event source mapping leaves the message
      visible after the queue's visibility timeout, redelivering it on
      the next poll. This is how the state machine ticks forward
      between agent completion / webhook events.
    * **Returned as a successful record** (terminal, orphan, or malformed
      beacons) — Lambda auto-deletes those messages on success, so the
      beacon is gone for good.

    Requires ``function_response_types=["ReportBatchItemFailures"]`` on
    the event source mapping. The beacon queue has no DLQ + no
    ``maxReceiveCount`` cap — a beacon cycles indefinitely until the
    run reaches a terminal state. SQS-level pathology surfaces via
    CloudWatch alarms on receive-count age, not via DLQ-by-redelivery.
    """
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []
    for record in records:
        if process_record(record):
            failures.append({"itemIdentifier": record["messageId"]})
    metrics.add_metric(name="BeaconsProcessed", unit=MetricUnit.Count, value=len(records))
    return {"batchItemFailures": failures}


def process_record(record: dict[str, Any]) -> bool:
    """Decode + dispatch one beacon.

    Returns ``True`` when the beacon should remain in the queue (active
    run, must keep cycling) and ``False`` when SQS should delete it
    (terminal / orphan / malformed). The handler reports the ``True``
    cases as ``batchItemFailures`` to keep them visible.
    """
    try:
        body = json.loads(record.get("body") or "{}")
    except json.JSONDecodeError:
        logger.warning("malformed beacon body", extra={"messageId": record.get("messageId")})
        return False
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        logger.warning("beacon missing run_id", extra={"body": body})
        return False
    run = read_run(run_id)
    if run is None:
        logger.info("orphan beacon — no run row", extra={"run_id": run_id})
        return False
    if run.current_state in TERMINAL_RUN_STATES:
        logger.info(
            "terminal run, releasing beacon",
            extra={"run_id": run_id, "state": str(run.current_state)},
        )
        return False
    action = decide(run)
    execute(run, action)
    return True


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


# Lambda values defer the function lookup so executors defined below
# this dict can be referenced (forward ref). The lambdas that drop
# ``_run`` adapt single-arg executors to the (run, action) signature
# the dispatcher expects; the ``GuardedAdvance`` lambda passes both
# args through and is wrapped only for the same forward-ref reason.
EXECUTORS: dict[type[Action], Any] = {
    Noop: execute_noop,
    InvokeAgent: lambda run, a: execute_invoke_agent(run, a),  # noqa: PLW0108
    EmitEvent: execute_emit_event,
    InvokeRepoHelper: lambda _run, a: execute_invoke_repo_helper(a),
    WriteSyntheticSpec: lambda _run, a: execute_write_synthetic_spec(a),
    SeedTasks: lambda _run, a: execute_seed_tasks(a),
    AdvanceState: lambda _run, a: execute_advance_state(a),
    GuardedAdvance: lambda run, a: execute_guarded_advance(run, a),  # noqa: PLW0108
}


def execute_invoke_agent(run: Run, action: InvokeAgent) -> None:
    """Optionally advance state, then fire the agent.

    When ``advance_from`` / ``advance_to`` / ``target_pk`` / ``target_sk``
    are all set, the advance is the per-invoke race guard. When all
    four are ``None`` the agent fires unconditionally — used for
    advisors gated by an outer :class:`GuardedAdvance`.

    Before any of that, the per-row dispatch circuit breaker is
    checked: when ``dispatch_failure_count >= MAX_DISPATCH_FAILURES``
    on the addressed row, the dispatch is suppressed and the
    appropriate breaker event (``TASK.BLOCKED`` or ``RUN.FAILED``) is
    emitted instead. This bounds the rollback-redeliver loop that
    would otherwise burn cost on a deterministically-failing agent.

    If :func:`dispatch_to_runtime` reports a synchronous dispatch failure
    (4xx / 5xx from the runtime — the agent never received the work, or
    the agent's container raised before doing any work), the state
    advance is rolled back so the next beacon cycle re-dispatches from
    the original state. The rollback also bumps
    ``dispatch_failure_count`` atomically so the breaker eventually
    trips.

    Asynchronous agent crashes (runtime accepted the work, agent
    started, then died mid-execution) aren't visible from this path —
    those need the stuck-run detector to recover.
    """
    if circuit_breaker_open(run, action):
        return
    if not invoke_advance_succeeds(action):
        return
    if dispatch_to_runtime(
        runtime_arn=action.runtime_arn,
        runtime_session_id=action.runtime_session_id,
        payload=action.payload,
    ):
        return
    rollback_invoke_advance(action)


def circuit_breaker_open(run: Run, action: InvokeAgent) -> bool:
    """Return ``True`` when the per-row dispatch breaker should suppress the dispatch.

    Reads ``dispatch_failure_count`` from the addressed row (already
    parsed off the run by :func:`read_run`). When the count is at or
    above :data:`MAX_DISPATCH_FAILURES`, emits the appropriate breaker
    event (``TASK.BLOCKED`` if the action targets a task with a PR;
    ``RUN.FAILED`` otherwise) and returns ``True``.

    Actions without an addressable row (``target_pk``/``target_sk``
    ``None`` — advisors gated by an outer :class:`GuardedAdvance`) are
    not subject to the per-row breaker.
    """
    if action.target_pk is None or action.target_sk is None:
        return False
    failure_count = lookup_dispatch_failure_count(run, action.target_sk)
    if failure_count < MAX_DISPATCH_FAILURES:
        return False
    logger.warning(
        "circuit breaker tripped — suppressing dispatch",
        extra={
            "target_sk": action.target_sk,
            "dispatch_failure_count": failure_count,
            "max_dispatch_failures": MAX_DISPATCH_FAILURES,
        },
    )
    metrics.add_metric(name="DispatchCircuitTripped", unit=MetricUnit.Count, value=1)
    emit_circuit_breaker_event(run, action, failure_count)
    return True


def lookup_dispatch_failure_count(run: Run, target_sk: str) -> int:
    """Resolve the breaker counter for the addressed row off the parsed run."""
    if target_sk == "STATE":
        return run.dispatch_failure_count
    if target_sk.startswith("TASK#"):
        task_id = target_sk.removeprefix("TASK#")
        for task in run.tasks:
            if task.task_id == task_id:
                return task.dispatch_failure_count
    return 0


def emit_circuit_breaker_event(run: Run, action: InvokeAgent, count: int) -> None:
    """Emit ``TASK.BLOCKED`` (task with PR) or ``RUN.FAILED`` (otherwise).

    ``TaskBlocked`` requires a ``pr_url`` because the human-recovery
    surface is a PR comment. When the breaker trips before the
    implementer ever produced a PR, fall back to ``RUN.FAILED`` so the
    run terminates and surfaces in the dashboard rather than wedging
    forever.
    """
    target_sk = action.target_sk or ""
    if target_sk.startswith("TASK#") and run.spec_slug:
        task_id = target_sk.removeprefix("TASK#")
        task = next((t for t in run.tasks if t.task_id == task_id), None)
        if task is not None and task.pr_url:
            emit_breaker_task_blocked(run, task, count, action.runtime_session_id)
            return
    emit_breaker_run_failed(run, action, count)


def emit_breaker_task_blocked(
    run: Run,
    task: Task,
    count: int,
    runtime_session_id: str,
) -> None:
    """Emit ``TASK.BLOCKED`` on circuit-trip — humans drive recovery via the PR."""
    if not run.spec_slug or not task.pr_url:
        return
    envelope = EventEnvelope[TaskBlocked](
        event_id=new_event_id(),
        type="TASK.BLOCKED",
        run_id=RunId(run.run_id),
        correlation_id=CorrelationId(run.correlation_id),
        actor_id="state_router",
        payload=TaskBlocked(
            project_slug=run.project_slug,
            spec_slug=run.spec_slug,
            task_id=task.task_id,
            pr_url=task.pr_url,
            blocked_reason=(
                f"dispatch failed {count} times — circuit breaker tripped; "
                "comment on this PR to retry, close to abort"
            ),
            session_id=runtime_session_id,
        ),
    )
    publish(envelope)


def emit_breaker_run_failed(run: Run, action: InvokeAgent, count: int) -> None:
    """Emit ``RUN.FAILED`` on circuit-trip when no PR exists to comment on."""
    failed_state = str(run.current_state) if run.current_state is not None else "unknown"
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(run.run_id),
        correlation_id=CorrelationId(run.correlation_id),
        actor_id="state_router",
        payload=RunFailed(
            project_slug=run.project_slug,
            failed_state=failed_state,
            error_class="dispatch_circuit_open",
            error_message=(
                f"agent dispatch failed {count} times for "
                f"{action.target_sk or 'unknown'}; "
                "see state_router CloudWatch logs"
            ),
            retryable=True,
        ),
    )
    publish(envelope)


def rollback_invoke_advance(action: InvokeAgent) -> None:
    """Reverse a state advance that we made before a failed dispatch.

    Conditional on the state still being at ``advance_to`` — if the
    projector has already moved the state forward (e.g., a stale
    completion event for a prior attempt landed), the rollback no-ops.

    The same conditional ``UpdateItem`` also increments
    ``dispatch_failure_count`` and stamps ``last_dispatch_failure_at``
    — both happen atomically with the state reversal so a successful
    rollback is the only path that bumps the breaker counter.
    """
    if (
        action.target_pk is None
        or action.target_sk is None
        or action.advance_from is None
        or action.advance_to is None
    ):
        return
    rolled_back = advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_to,
        advance_to=action.advance_from,
        extra_attrs={"last_dispatch_failure_at": now_iso()},
        extra_increments={"dispatch_failure_count": 1},
    )
    if rolled_back:
        metrics.add_metric(
            name="DispatchFailureCount",
            unit=MetricUnit.Count,
            value=1,
        )
        logger.info(
            "rolled back state after dispatch failure",
            extra={
                "target_sk": action.target_sk,
                "from": action.advance_to,
                "to": action.advance_from,
            },
        )
    else:
        logger.info(
            "skipped rollback — state already moved",
            extra={"target_sk": action.target_sk, "advance_to": action.advance_to},
        )


def invoke_advance_succeeds(action: InvokeAgent) -> bool:
    """Run the optional per-invoke conditional advance.

    Returns ``True`` when the agent should fire — either because the
    advance succeeded or because the action carries no advance fields
    (gated by an outer :class:`GuardedAdvance`).
    """
    if (
        action.target_pk is None
        or action.target_sk is None
        or action.advance_from is None
        or action.advance_to is None
    ):
        return True
    won = advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_to,
    )
    if not won:
        logger.info(
            "lost dispatch race, skipping",
            extra={"target_sk": action.target_sk, "advance_to": action.advance_to},
        )
    return won


def execute_guarded_advance(run: Run, action: Action) -> None:
    """Atomic state advance; on success, run ``on_success`` actions.

    The advance is the race guard. If a concurrent router already
    advanced the state (the conditional update fails), we skip the
    follow-ups — the winning router will run them. Idempotent across
    redelivered beacons.
    """
    if not isinstance(action, GuardedAdvance):
        return
    won = advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_to,
    )
    if not won:
        logger.info(
            "lost guarded advance, skipping on_success",
            extra={
                "target_sk": action.target_sk,
                "advance_from": action.advance_from,
                "advance_to": action.advance_to,
            },
        )
        return
    for sub in action.on_success:
        execute(run, sub)


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
    """Extract the PR URL from a repo_helper open-PR response, if requested.

    repo_helper wraps every successful response as
    ``{"ok": true, "op": ..., "result": {...}}`` — so the PR URL lives
    under ``result.pr_url``, not at the top level.
    """
    if not action.record_pr_url_attr:
        return {}
    result = body.get("result") or {}
    pr_url = result.get("pr_url") if isinstance(result, dict) else None
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


def execute_seed_tasks(action: SeedTasks) -> None:
    """Write one TASK row per id in ``status=pending``.

    Conditional on ``attribute_not_exists(pk)`` so a redelivered beacon
    (or a router that lost the dispatch race) doesn't clobber a row that
    already advanced beyond ``pending``. ``ConditionalCheckFailedException``
    is treated as success — the row exists, that's what we wanted.
    """
    ts = now_iso()
    for task_id in action.task_ids:
        try:
            ddb().put_item(
                TableName=runs_table(),
                Item={
                    "pk": {"S": f"RUN#{action.run_id}"},
                    "sk": {"S": f"TASK#{task_id}"},
                    "status": {"S": "pending"},
                    "iteration_count": {"N": "0"},
                    "created_at": {"S": ts},
                    "updated_at": {"S": ts},
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise


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
    extra_increments: dict[str, int] | None = None,
) -> bool:
    """Conditionally update ``current_state`` (or task ``status``) → next.

    The condition checks the previous value to defend against
    concurrent routers. Returns ``True`` on success, ``False`` if the
    condition failed (another router advanced state first; we no-op).

    Run rows use the ``current_state`` attribute; task rows use
    ``status``. Picked by ``target_sk``.

    ``extra_attrs`` adds ``SET`` clauses (string values).
    ``extra_increments`` adds ``ADD`` clauses (integer deltas) — used
    by the rollback path to bump ``dispatch_failure_count`` atomically
    with the state reversal.
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
    add_parts: list[str] = []
    for i, (k, n) in enumerate(extra_increments.items() if extra_increments else ()):
        add_parts.append(f"#i{i} :n{i}")
        values[f":n{i}"] = {"N": str(n)}
        names[f"#i{i}"] = k
    expression = "SET " + ", ".join(set_parts)
    if add_parts:
        expression += " ADD " + ", ".join(add_parts)
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


def now_iso() -> str:
    """Tz-aware UTC ISO timestamp for ``updated_at``."""
    return datetime.now(UTC).isoformat()
