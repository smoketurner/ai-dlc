"""AgentCore Runtime entrypoint for the Implementer.

The state-router invokes this once per dispatch — first run or iteration.
The entrypoint validates the input, runs one Claude Agent SDK session,
emits ``TASK.READY`` with the PR URL, and returns the result.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, TaskReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ImplementerInput, ImplementerResult
from implementer.client import execute_task

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Run the implementer task and emit ``TASK.READY``.

    The router conditionally advanced the task state to
    ``implementer_running`` (or ``iterating`` for a re-dispatch) before
    invoking us. On TASK.READY the projector advances the task back to
    ``pr_open`` so the next beacon dispatches the advisors.
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
    logger.info(
        "task ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
        pr_url=result.pr_url,
    )
    if result.pr_url is not None:
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


if __name__ == "__main__":
    app.run()
