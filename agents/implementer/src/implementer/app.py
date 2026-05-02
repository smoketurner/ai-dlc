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
from implementer.client import execute_task

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Implementer entrypoint. Returns a JSON-serialisable ImplementerResult."""
    payload = ImplementerInput.model_validate(event)
    logger.info(
        "implementer invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        spec_slug=payload.spec_slug,
        retry=payload.prior_feedback is not None,
    )
    result = await execute_task(payload)
    logger.info(
        "task ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
        pr_url=result.pr_url,
    )
    return result.model_dump()


if __name__ == "__main__":
    app.run()
