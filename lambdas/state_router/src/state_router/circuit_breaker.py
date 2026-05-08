"""Per-row dispatch circuit breaker.

Each agent invoke targets a specific DDB row (the run STATE row or a
TASK# row). Every time a dispatch fails synchronously, the rollback
path bumps that row's ``dispatch_failure_count``. Once it crosses
:data:`~.config.MAX_DISPATCH_FAILURES`, this module suppresses further
dispatch on that row and emits a breaker event:

* ``TASK.BLOCKED`` when the task already has a PR — humans can comment
  on the PR to retry or close to abort.
* ``RUN.FAILED`` otherwise — the run terminates and surfaces in the
  dashboard so it doesn't wedge silently.

Successful agent completion (``*.READY`` event) resets the counter
back to 0 in the projector, so the breaker is per-streak, not lifetime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from common.event_emit import publish
from common.events import EventEnvelope, RunFailed, TaskBlocked
from common.ids import CorrelationId, RunId, new_event_id
from state_router.config import MAX_DISPATCH_FAILURES

if TYPE_CHECKING:
    from state_router.actions import InvokeAgent
    from state_router.model import Run, Task

logger = Logger(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")


def is_open(run: Run, action: InvokeAgent) -> bool:
    """Return ``True`` when the per-row dispatch breaker should suppress the dispatch.

    Reads ``dispatch_failure_count`` from the addressed row (already
    parsed off the run by :func:`~.handler.read_run`). When the count
    is at or above :data:`~.config.MAX_DISPATCH_FAILURES`, emits the
    appropriate breaker event (``TASK.BLOCKED`` if the action targets
    a task with a PR; ``RUN.FAILED`` otherwise) and returns ``True``.

    Actions without an addressable row (``target_pk``/``target_sk``
    ``None`` — advisors gated by an outer :class:`~.actions.GuardedAdvance`)
    are not subject to the per-row breaker.
    """
    if action.target_pk is None or action.target_sk is None:
        return False
    count = failure_count(run, action.target_sk)
    if count < MAX_DISPATCH_FAILURES:
        return False
    logger.warning(
        "circuit breaker tripped — suppressing dispatch",
        extra={
            "target_sk": action.target_sk,
            "dispatch_failure_count": count,
            "max_dispatch_failures": MAX_DISPATCH_FAILURES,
        },
    )
    metrics.add_metric(name="DispatchCircuitTripped", unit=MetricUnit.Count, value=1)
    emit_tripped(run, action, count)
    return True


def failure_count(run: Run, target_sk: str) -> int:
    """Resolve the breaker counter for the addressed row off the parsed run."""
    if target_sk == "STATE":
        return run.dispatch_failure_count
    if target_sk.startswith("TASK#"):
        task_id = target_sk.removeprefix("TASK#")
        for task in run.tasks:
            if task.task_id == task_id:
                return task.dispatch_failure_count
    return 0


def emit_tripped(run: Run, action: InvokeAgent, count: int) -> None:
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
            emit_task_blocked(run, task, count, action.runtime_session_id)
            return
    emit_run_failed(run, action, count)


def emit_task_blocked(
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


def emit_run_failed(run: Run, action: InvokeAgent, count: int) -> None:
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
