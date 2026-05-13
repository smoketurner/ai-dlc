"""Per-row dispatch circuit breaker.

Each agent invoke targets the run STATE row (there are no TASK rows
any more in the single-PR-per-issue world). Every time a dispatch
fails synchronously, the rollback path bumps that row's
``dispatch_failure_count``. Once it crosses
:data:`~.config.MAX_DISPATCH_FAILURES`, this module suppresses
further dispatch on that row and emits ``RUN.FAILED`` so the run
terminates rather than wedging silently.

Successful agent completion (``*.READY`` event) resets the counter
back to 0 in the projector, so the breaker is per-streak, not lifetime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from common.event_emit import publish
from common.events import EventEnvelope, RunFailed
from common.ids import CorrelationId, RunId, new_event_id
from state_router.config import MAX_DISPATCH_FAILURES

if TYPE_CHECKING:
    from state_router.actions import InvokeAgent, InvokeRepoHelper
    from state_router.model import Run

logger = Logger(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")


def is_open(run: Run, action: InvokeAgent | InvokeRepoHelper) -> bool:
    """Return ``True`` when the per-row dispatch breaker should suppress the dispatch.

    Reads ``dispatch_failure_count`` from the addressed STATE row
    (already parsed off the run by :func:`~.handler.read_run`). When
    the count is at or above :data:`~.config.MAX_DISPATCH_FAILURES`,
    emits ``RUN.FAILED`` and returns ``True``.

    Actions without an addressable row (``target_pk``/``target_sk``
    ``None`` — parallel validator invokes or informational repo_helper
    ops like ``comment_issue``) are not subject to the per-row
    breaker.
    """
    if action.target_pk is None or action.target_sk is None:
        return False
    if run.dispatch_failure_count < MAX_DISPATCH_FAILURES:
        return False
    logger.warning(
        "circuit breaker tripped — suppressing dispatch",
        extra={
            "target_sk": action.target_sk,
            "dispatch_failure_count": run.dispatch_failure_count,
            "max_dispatch_failures": MAX_DISPATCH_FAILURES,
        },
    )
    metrics.add_metric(name="DispatchCircuitTripped", unit=MetricUnit.Count, value=1)
    emit_run_failed(run, action, run.dispatch_failure_count)
    return True


def emit_run_failed(run: Run, action: InvokeAgent | InvokeRepoHelper, count: int) -> None:
    """Emit ``RUN.FAILED`` on circuit-trip — the run terminates and surfaces in the dashboard."""
    from common.state import RunState  # noqa: PLC0415 - local import to avoid cycle

    failed_state = (
        str(run.current_state) if run.current_state is not None else RunState.failed.value
    )
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
                f"dispatch failed {count} times for "
                f"{action.target_sk or 'unknown'}; "
                "see state_router CloudWatch logs"
            ),
            retryable=True,
        ),
    )
    publish(envelope)
