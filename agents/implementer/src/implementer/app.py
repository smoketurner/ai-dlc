"""AgentCore Runtime entrypoint for the Implementer.

Step Functions invokes this once per task in the spec's tasks.md. The
entrypoint validates the input, drives one Claude Agent SDK session, and
returns an ``ImplementerResult`` for the TASK.READY event payload.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import ImplementerInput
from common.task_token import heartbeat_loop, send_task_failure, send_task_success
from implementer.client import execute_task

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Implementer entrypoint.

    When ``task_token`` is set on the payload, the call originated from
    ``runtime_invoker`` and Step Functions is waiting on a token callback
    rather than the HTTP response body — typical for long-running task
    executions (clone + Claude Code + push regularly takes minutes). We
    do the work, then call ``SendTaskSuccess`` / ``SendTaskFailure``
    directly, and the HTTP response body is ignored.

    When ``task_token`` is absent, the call is synchronous (e.g. a smoke
    test invocation) and we just return the result.
    """
    payload = ImplementerInput.model_validate(event)
    logger.info(
        "implementer invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        spec_slug=payload.spec_slug,
        retry=payload.prior_feedback is not None,
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
    return output


if __name__ == "__main__":
    app.run()
