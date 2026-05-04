"""AgentCore Runtime entrypoint for the Critic.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. Step Functions
calls the runtime, which invokes the entrypoint defined here. The
entrypoint:

  1. Validates the input as :class:`CriticInput`.
  2. Asks the Strands agent for a :class:`Critique`.
  3. Renders the critique as Markdown and uploads it to S3 — deterministic
     even if the model forgets to call a write tool.
  4. Returns a :class:`CriticResult` for the CRITIQUE.READY event payload.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import CriticInput, CriticResult
from critic.agent import critique_spec
from critic.critique import Critique, render_critique, severity_counts
from critic.tools import critique_s3_key, write_critique

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Critic entrypoint. Returns a JSON-serialisable CriticResult."""
    payload = CriticInput.model_validate(event)
    logger.info(
        "critic invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        spec_slug=payload.spec_slug,
    )

    critique = critique_spec(
        project_slug=payload.project_slug,
        spec_slug=payload.spec_slug,
        intent=payload.intent,
    )
    upload_critique(critique, run_id=payload.run_id)

    counts = severity_counts(critique)
    result = CriticResult(
        spec_slug=critique.spec_slug,
        critique_s3_key=critique_s3_key(payload.run_id),
        issue_count=len(critique.issues),
        high_severity_count=counts["high"],
        medium_severity_count=counts["medium"],
        low_severity_count=counts["low"],
        summary=critique.summary[:2048],
        session_id=payload.run_id,
    )
    logger.info(
        "critique ready",
        run_id=payload.run_id,
        spec_slug=critique.spec_slug,
        issue_count=result.issue_count,
        high=result.high_severity_count,
    )
    return result.model_dump()


def upload_critique(critique: Critique, *, run_id: str) -> None:
    """Render and upload the critique Markdown to S3."""
    write_critique(run_id, render_critique(critique))


if __name__ == "__main__":
    app.run()
