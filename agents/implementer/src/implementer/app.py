"""AgentCore Runtime entrypoint for the Implementer.

Step Functions invokes this once per task in the spec's tasks.md. The
iteration_reactor invokes this on iteration runs (``iteration_count > 0``,
no SF task_token). The entrypoint validates the input, drives one Claude
Agent SDK session, and returns an ``ImplementerResult`` — emitting
``TASK.ITERATION_COMMITTED`` directly when invoked by the reactor since
there's no SFN ``PublishTaskReady`` state to do it for us.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, TaskIterationCommitted
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ImplementerInput, ImplementerResult
from common.task_token import heartbeat_loop, send_task_failure, send_task_success
from implementer.client import execute_task
from implementer.repo_ops import run_git

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Implementer entrypoint.

    Two invocation modes share this handler:

    * **SFN-driven** (``task_token`` set): runtime_invoker dispatched us;
      Step Functions is waiting on a token callback. We call
      ``SendTaskSuccess`` / ``SendTaskFailure`` directly when done.
    * **Reactor-driven** (``iteration_count > 0``, no token): the
      iteration_reactor dispatched us to address PR feedback. We publish
      ``TASK.ITERATION_COMMITTED`` ourselves so the reactor can run
      Reviewer + Tester against the new commit.
    """
    payload = ImplementerInput.model_validate(event)
    logger.info(
        "implementer invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        spec_slug=payload.spec_slug,
        iteration=payload.iteration_count,
        async_token=payload.task_token is not None,
    )
    try:
        with heartbeat_loop(payload.task_token):
            result = await execute_task(payload)
    except BaseException as exc:
        if payload.task_token is not None:
            send_task_failure(task_token=payload.task_token, exc=exc)
            return {"task_token_dispatched": True, "ok": False}
        raise
    logger.info(
        "task ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
        pr_url=result.pr_url,
    )
    output = result.model_dump()
    if payload.task_token is not None:
        send_task_success(task_token=payload.task_token, output=output)
        return {"task_token_dispatched": True, "ok": True}
    if payload.iteration_count > 0 and result.pr_url is not None:
        publish_iteration_committed(payload, result, pr_url=result.pr_url)
    return output


def publish_iteration_committed(
    payload: ImplementerInput,
    result: ImplementerResult,
    *,
    pr_url: str,
) -> None:
    """Emit TASK.ITERATION_COMMITTED so the reactor dispatches Reviewer + Tester.

    Called only on the reactor-driven path (no task_token, iteration > 0).
    The caller has already confirmed ``result.pr_url is not None`` and
    passes it as ``pr_url`` so the type checker doesn't have to narrow
    again here.
    """
    head_sha = run_git("rev-parse", "HEAD").strip()
    inline_count = sum(
        1 for item in (payload.iteration_feedback or []) if hasattr(item, "comment_id")
    )
    envelope = EventEnvelope[TaskIterationCommitted](
        event_id=new_event_id(),
        type="TASK.ITERATION_COMMITTED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="implementer",
        payload=TaskIterationCommitted(
            project_slug=payload.project_slug,
            spec_slug=payload.spec_slug,
            task_id=payload.task_id,
            pr_url=pr_url,
            iteration_count=payload.iteration_count,
            head_sha=head_sha,
            inline_replies_count=inline_count,
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
