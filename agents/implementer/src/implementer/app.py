"""AgentCore Runtime entrypoint for the Implementer.

The state-router invokes this once per dispatch — first run or iteration.
The entrypoint:

  1. Validates the input as :class:`ImplementerInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the Claude Agent SDK session runs.
  3. Spawns a daemon thread that runs one Claude Agent SDK session,
     emits ``TASK.READY`` (real implementation), ``TASK.BLOCKED``
     (the agent could not produce a diff and needs human guidance),
     or ``RUN.FAILED`` (uncaught exception, or the agent produced
     no PR), and acknowledges the async task.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms so the state-router doesn't wait for the agent's full
     runtime and AgentCore's frontend never retries the dispatch.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, RunFailed, TaskBlocked, TaskReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ImplementerInput, ImplementerResult
from implementer.client import execute_task

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately.

    The router has already advanced the task state to
    ``implementer_running`` (or ``iterating`` for a re-dispatch). The
    background thread emits ``TASK.READY`` → ``pr_open`` (advisors
    fire next), ``TASK.BLOCKED`` → ``blocked`` (waits for a human
    comment on the draft PR), or ``RUN.FAILED`` if the agent crashed
    or produced no PR.
    """
    payload = ImplementerInput.model_validate(event)
    logger.info(
        "implementer invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        spec_slug=payload.spec_slug,
        iteration=payload.iteration_count,
    )
    task_id = app.add_async_task(
        "implementer_run",
        {"run_id": payload.run_id, "task_id": payload.task_id},
    )
    threading.Thread(
        target=run_implementer,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {
        "status": "dispatched",
        "run_id": payload.run_id,
        "task_id": payload.task_id,
        "async_task_id": task_id,
    }


def run_implementer(payload: ImplementerInput, async_task_id: int) -> None:
    """Body of the implementer run — invokes Claude Agent SDK, emits event.

    Runs in a daemon thread spawned from :func:`handler`. ``execute_task``
    is async (the Claude Agent SDK driver is awaitable), so the body
    runs it under :func:`asyncio.run`.
    """
    try:
        result = asyncio.run(execute_task(payload))
        emit_terminal_event(payload, result)
    except Exception as exc:
        logger.exception(
            "implementer run failed",
            run_id=payload.run_id,
            task_id=payload.task_id,
        )
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(async_task_id)


def emit_terminal_event(payload: ImplementerInput, result: ImplementerResult) -> None:
    """Branch on the agent's result to emit TASK.READY / TASK.BLOCKED.

    The implementer no longer owns the PR — the unified impl PR is
    opened by the state router on the first task event. ``pr_url`` on
    the event is left empty; the projector / state router backfills
    it once the PR is open.
    """
    if result.blocked_reason is not None:
        logger.info(
            "task blocked",
            run_id=payload.run_id,
            task_id=payload.task_id,
            blocked_reason=result.blocked_reason,
        )
        emit_task_blocked(payload, result)
        return
    logger.info(
        "task ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
    )
    emit_task_ready(payload, result)


def emit_task_ready(payload: ImplementerInput, result: ImplementerResult) -> None:
    """Emit TASK.READY so the projector advances the task to ``pr_open``."""
    envelope = EventEnvelope[TaskReady](
        event_id=new_event_id(),
        type="TASK.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=TaskReady(
            project_slug=payload.project_slug,
            spec_slug=payload.spec_slug,
            task_id=payload.task_id,
            diff_summary=result.diff_summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def emit_task_blocked(payload: ImplementerInput, result: ImplementerResult) -> None:
    """Emit TASK.BLOCKED so the projector advances the task to ``blocked``."""
    if result.blocked_reason is None:
        msg = "emit_task_blocked called without blocked_reason"
        raise ValueError(msg)
    envelope = EventEnvelope[TaskBlocked](
        event_id=new_event_id(),
        type="TASK.BLOCKED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=TaskBlocked(
            project_slug=payload.project_slug,
            spec_slug=payload.spec_slug,
            task_id=payload.task_id,
            blocked_reason=result.blocked_reason,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def publish_run_failed(payload: ImplementerInput, exc: BaseException) -> None:
    """Emit RUN.FAILED on uncaught exception in the agent body."""
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="implementer_running",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
