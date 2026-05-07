"""AgentCore Runtime entrypoint for the Implementer.

The state-router invokes this once per dispatch — first run or iteration.
The entrypoint validates the input, runs one Claude Agent SDK session,
emits ``TASK.READY`` (real implementation) or ``TASK.BLOCKED`` (the
agent could not produce a diff and needs human guidance), and returns
the result.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, TaskBlocked, TaskReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ImplementerInput, ImplementerResult
from implementer.client import execute_task

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Run the implementer task and emit ``TASK.READY`` or ``TASK.BLOCKED``.

    The router advanced the task state to ``implementer_running`` (or
    ``iterating`` for a re-dispatch) before invoking us. The next event
    advances the task per ``common.state_transitions.TASK_TRANSITIONS``:
    ``TASK.READY`` → ``pr_open`` (advisors fire next), ``TASK.BLOCKED``
    → ``blocked`` (waits for a human comment on the draft PR).
    """
    payload = ImplementerInput.model_validate(event)
    logger.info(
        "implementer invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        spec_slug=payload.spec_slug,
        iteration=payload.iteration_count,
    )
    result = await execute_task(payload)
    if result.pr_url is None:
        logger.error(
            "implementer returned no pr_url",
            run_id=payload.run_id,
            task_id=payload.task_id,
        )
        return result.model_dump()
    if result.blocked_reason is not None:
        logger.info(
            "task blocked",
            run_id=payload.run_id,
            task_id=payload.task_id,
            pr_url=result.pr_url,
            blocked_reason=result.blocked_reason,
        )
        emit_task_blocked(payload, result, pr_url=result.pr_url)
    else:
        logger.info(
            "task ready",
            run_id=payload.run_id,
            task_id=payload.task_id,
            pr_url=result.pr_url,
        )
        emit_task_ready(payload, result, pr_url=result.pr_url)
    return result.model_dump()


def emit_task_ready(
    payload: ImplementerInput,
    result: ImplementerResult,
    *,
    pr_url: str,
) -> None:
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
            pr_url=pr_url,
            diff_summary=result.diff_summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def emit_task_blocked(
    payload: ImplementerInput,
    result: ImplementerResult,
    *,
    pr_url: str,
) -> None:
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
            pr_url=pr_url,
            blocked_reason=result.blocked_reason,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
