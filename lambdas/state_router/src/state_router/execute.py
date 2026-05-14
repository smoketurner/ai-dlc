"""Action executors — apply the side effects of every :data:`Action`.

The handler walks the action returned by :func:`~.dispatch.decide`
and dispatches by type via :data:`EXECUTORS`. Each executor wraps
one of the AWS primitives in :mod:`state_router.aws` (``advance_state``
for state moves, ``dispatch_to_runtime`` for AgentCore invokes,
``lambda_client`` for repo_helper invokes).

Both :class:`~.actions.InvokeAgent` and :class:`~.actions.InvokeRepoHelper`
participate in the per-row circuit breaker + retry loop. The agent
flow advances state *before* the runtime call (race guard) and
rolls back on failure; the repo_helper flow advances *after* a
successful response and bumps ``dispatch_failure_count`` + enqueues
a retry beacon on failure (no state to revert). Both paths trip the
breaker after :data:`~.config.MAX_DISPATCH_FAILURES` consecutive
failures so a deterministically-failing op surfaces as ``RUN.FAILED``
instead of wedging.
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from common.event_emit import publish
from state_router import circuit_breaker
from state_router.actions import (
    Action,
    AdvanceState,
    CompoundAction,
    EmitEvent,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
)
from state_router.aws import (
    dispatch_to_runtime,
    lambda_client,
    now_iso,
    transactional_advance,
)
from state_router.config import (
    repo_helper_function_name,
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
    four are ``None`` the agent fires unconditionally (used for the
    parallel validators in ``handle_impl_pr_open``; the surrounding
    AdvanceState owns the race guard for the whole compound).

    Before any of that, the per-row dispatch circuit breaker is
    checked: when ``dispatch_failure_count >= MAX_DISPATCH_FAILURES``
    on the addressed row, the dispatch is suppressed and ``RUN.FAILED``
    is emitted instead. This bounds the rollback-redeliver loop that
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
    if not try_advance(run, action):
        return
    if dispatch_to_runtime(
        runtime_arn=action.runtime_arn,
        runtime_session_id=action.runtime_session_id,
        runtime_user_id=action.runtime_user_id,
        payload=action.payload,
    ):
        return
    rollback_after_failure(run, action)


def try_advance(run: Run, action: InvokeAgent) -> bool:
    """Run the optional per-invoke conditional advance.

    Returns ``True`` when the agent should fire — either because the
    advance succeeded or because the action carries no advance fields
    (gated by an outer AdvanceState in the same compound).
    """
    if (
        action.target_pk is None
        or action.target_sk is None
        or action.advance_from is None
        or action.advance_to is None
    ):
        return True
    won = transactional_advance(
        run_id=run.run_id,
        project_slug=run.project_slug,
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


def rollback_after_failure(run: Run, action: InvokeAgent) -> None:
    """Reverse a state advance that we made before a failed dispatch.

    Conditional on the state still being at ``advance_to`` — if the
    projector has already moved the state forward (e.g., a stale
    completion event for a prior attempt landed), the rollback no-ops.

    The same conditional ``UpdateItem`` also increments
    ``dispatch_failure_count`` and stamps ``last_dispatch_failure_at``
    — both happen atomically with the state reversal so a successful
    rollback is the only path that bumps the breaker counter.

    The rollback and the retry-outbox row are written in a single
    ``TransactWriteItems`` so the state revert and the beacon-intent
    row commit together. The pipe forwards the outbox row to the
    beacon queue just like the projector's happy-path rows; without it
    the run would wedge — the rollback fires no event, so nothing
    else wakes the router. The per-row ``dispatch_failure_count``
    eventually trips the circuit breaker, which suppresses dispatch
    in :func:`circuit_breaker.is_open` and emits the breaker event
    instead.
    """
    if (
        action.target_pk is None
        or action.target_sk is None
        or action.advance_from is None
        or action.advance_to is None
    ):
        return
    # Reverse advance + counter bump in one atomic transaction with
    # the retry OUTBOX row, so the pipe always sees the rollback
    # through. ``advance_from`` and ``advance_to`` are swapped: the
    # condition guards that the state is still where the failed
    # dispatch left it.
    rolled_back = transactional_advance(
        run_id=run.run_id,
        project_slug=run.project_slug,
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


def execute_invoke_repo_helper(run: Run, action: InvokeRepoHelper) -> None:
    """Synchronous Lambda invoke; advance state on success, retry on failure.

    The breaker check at the top suppresses dispatch on rows whose
    ``dispatch_failure_count`` has crossed
    :data:`~.config.MAX_DISPATCH_FAILURES` — same gate as the agent path.

    On a failed response (``ok: false``) for an action carrying advance
    fields, :func:`record_repo_helper_failure` bumps the counter and
    writes an OUTBOX row in one transaction so the EventBridge Pipe
    enqueues a fresh retry beacon. Informational ops without advance
    fields (``comment_issue`` / ``label_issue``) just log and return —
    they're chained with a follow-up action that runs regardless.
    """
    if circuit_breaker.is_open(run, action):
        return
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
        record_repo_helper_failure(run, action)
        return
    if not action.target_pk or not action.advance_from:
        return
    advance_to = pick_advance_to(action, body)
    if advance_to is None:
        return
    extra_attrs = build_extra_attrs(action, body)
    transactional_advance(
        run_id=run.run_id,
        project_slug=run.project_slug,
        target_pk=action.target_pk,
        target_sk=action.target_sk or "STATE",
        advance_from=action.advance_from,
        advance_to=advance_to,
        extra_attrs=extra_attrs,
    )


def record_repo_helper_failure(run: Run, action: InvokeRepoHelper) -> None:
    """Bump ``dispatch_failure_count`` + enqueue a retry beacon.

    Mirrors the agent path's :func:`rollback_after_failure` but skips the
    state revert — the repo_helper executor advances state *after* a
    successful response, so on failure there's nothing to reverse.
    Reuses :func:`transactional_advance` with ``advance_from`` ==
    ``advance_to``: the SET is a no-op, the conditional check still
    acts as the race guard (only bump if the projector hasn't moved us
    forward via a stale event), and the OUTBOX row commits in the same
    transaction so the EventBridge Pipe enqueues the next beacon.

    Informational ops without ``target_pk`` / ``advance_from``
    (``comment_issue`` / ``label_issue``) skip entirely — there's no
    race guard to use as a conditional check, and they're chained with
    a follow-up action that fires regardless of whether the GitHub call
    succeeded.
    """
    if not action.target_pk or not action.target_sk or not action.advance_from:
        return
    bumped = transactional_advance(
        run_id=run.run_id,
        project_slug=run.project_slug,
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_from,
        extra_attrs={"last_dispatch_failure_at": now_iso()},
        extra_increments={"dispatch_failure_count": 1},
    )
    if bumped:
        metrics.add_metric(name="DispatchFailureCount", unit=MetricUnit.Count, value=1)
        logger.info(
            "recorded repo_helper failure; retry beacon enqueued",
            extra={"target_sk": action.target_sk, "op": action.op},
        )
    else:
        logger.info(
            "skipped repo_helper failure record — state already moved",
            extra={"target_sk": action.target_sk, "op": action.op},
        )


def pick_advance_to(action: InvokeRepoHelper, body: dict[str, Any]) -> str | None:
    """Choose the advance target based on the op result.

    When the result carries ``no_change: true``, use
    ``advance_on_no_change_to`` if set; otherwise fall through to the
    normal ``advance_to``. (No router op currently emits ``no_change``
    in the single-PR-per-issue world — the field is kept for forward
    compatibility.)
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
    if not action.record_pr_url_attrs:
        return {}
    result = body.get("result") or {}
    pr_url = result.get("pr_url") if isinstance(result, dict) else None
    if not isinstance(pr_url, str) or not pr_url:
        return {}
    return dict.fromkeys(action.record_pr_url_attrs, pr_url)


def execute_advance_state(run: Run, action: AdvanceState) -> None:
    """Pure conditional state advance (no other side effect).

    ``action.extra_attrs`` is forwarded so attributes such as
    ``last_revision_trigger`` land atomically with the state transition
    (the same DDB transaction also writes the OUTBOX row that fires the
    follow-up beacon).
    """
    transactional_advance(
        run_id=run.run_id,
        project_slug=run.project_slug,
        target_pk=action.target_pk,
        target_sk=action.target_sk,
        advance_from=action.advance_from,
        advance_to=action.advance_to,
        extra_attrs=dict(action.extra_attrs) if action.extra_attrs else None,
    )


# Lambda values defer the function lookup so executors defined below
# this dict can be referenced (forward ref). Every state-mutating
# executor takes ``run`` so it can write the OUTBOX row alongside
# the state advance via :func:`transactional_advance`.
EXECUTORS: dict[type[Action], Any] = {
    Noop: execute_noop,
    InvokeAgent: lambda run, a: execute_invoke_agent(run, a),  # noqa: PLW0108
    EmitEvent: execute_emit_event,
    InvokeRepoHelper: lambda run, a: execute_invoke_repo_helper(run, a),  # noqa: PLW0108
    AdvanceState: lambda run, a: execute_advance_state(run, a),  # noqa: PLW0108
}
