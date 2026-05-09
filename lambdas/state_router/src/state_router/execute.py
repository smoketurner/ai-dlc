"""Action executors — apply the side effects of every :data:`Action`.

The handler walks the action returned by :func:`~.dispatch.decide`
and dispatches by type via :data:`EXECUTORS`. Each executor wraps
one of the AWS primitives in :mod:`state_router.aws` (``advance_state``
for state moves, ``dispatch_to_runtime`` for AgentCore invokes,
``ddb`` for direct writes, ``s3`` for synthetic-spec uploads,
``lambda_client`` for repo_helper invokes).

The :class:`~.actions.InvokeAgent` flow is the involved one — it
checks the per-row circuit breaker first, runs an optional
conditional advance as the dispatch race guard, fires the runtime,
and rolls back the advance (atomically bumping
``dispatch_failure_count``) if the runtime call failed
synchronously. See :func:`execute_invoke_agent` for the sequence.
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from common.event_emit import publish
from state_router import circuit_breaker
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
from state_router.aws import (
    advance_state,
    ddb,
    dispatch_to_runtime,
    lambda_client,
    now_iso,
    s3,
)
from state_router.config import (
    artifacts_bucket,
    repo_helper_function_name,
    runs_table,
)
from state_router.model import Run

logger = Logger(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")


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

    If :func:`~.aws.dispatch_to_runtime` reports a synchronous
    dispatch failure (4xx / 5xx from the runtime — the agent never
    received the work, or the agent's container raised before doing
    any work), the state advance is rolled back so the next beacon
    cycle re-dispatches from the original state. The rollback also
    bumps ``dispatch_failure_count`` atomically so the breaker
    eventually trips.

    Asynchronous agent crashes (runtime accepted the work, agent
    started, then died mid-execution) aren't visible from this path —
    those need the stuck-run detector to recover.
    """
    if circuit_breaker.is_open(run, action):
        return
    if not try_advance(action):
        return
    if dispatch_to_runtime(
        runtime_arn=action.runtime_arn,
        runtime_session_id=action.runtime_session_id,
        payload=action.payload,
    ):
        return
    rollback_after_failure(action)


def try_advance(action: InvokeAgent) -> bool:
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


def rollback_after_failure(action: InvokeAgent) -> None:
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
    fn = repo_helper_function_name()
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
    if not action.target_pk or not action.advance_from:
        return
    advance_to = pick_advance_to(action, body)
    if advance_to is None:
        return
    extra_attrs = build_extra_attrs(action, body)
    advance_state(
        target_pk=action.target_pk,
        target_sk=action.target_sk or "STATE",
        advance_from=action.advance_from,
        advance_to=advance_to,
        extra_attrs=extra_attrs,
    )


def pick_advance_to(action: InvokeRepoHelper, body: dict[str, Any]) -> str | None:
    """Choose the advance target based on the op result.

    When the result carries ``no_change: true`` (currently only
    ``open_spec_pr`` emits this — same docs already on ``base``), use
    ``advance_on_no_change_to`` if set; otherwise fall through to the
    normal ``advance_to``.
    """
    result = body.get("result") or {}
    if isinstance(result, dict) and result.get("no_change") and action.advance_on_no_change_to:
        return action.advance_on_no_change_to
    return action.advance_to


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
