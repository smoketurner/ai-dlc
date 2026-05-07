"""AgentCore Runtime entrypoint for the Critic.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The state-router
Lambda invokes this runtime fire-and-forget when a run reaches
``critic_running``. The entrypoint:

  1. Validates the input as :class:`CriticInput`.
  2. Asks the Strands agent for a :class:`Critique`.
  3. Renders the critique as Markdown and uploads it to S3 — deterministic
     even if the model forgets to call a write tool.
  4. Emits ``CRITIQUE.READY`` so the projector advances the run, then
     returns the result body.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import CritiqueReady, EventEnvelope
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import CriticInput, CriticResult, usage_from_strands
from critic.agent import build_agent, critique_spec, model_id
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

    agent = build_agent(payload.run_id)
    critique = critique_spec(
        agent,
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
        **usage_from_strands(agent, model_id=model_id()),
    )
    logger.info(
        "critique ready",
        run_id=payload.run_id,
        spec_slug=critique.spec_slug,
        issue_count=result.issue_count,
        high=result.high_severity_count,
    )
    publish_critique_ready(payload, result)
    return result.model_dump()


def publish_critique_ready(payload: CriticInput, result: CriticResult) -> None:
    """Emit CRITIQUE.READY so the projector advances the run to ``spec_critiqued``."""
    envelope = EventEnvelope[CritiqueReady](
        event_id=new_event_id(),
        type="CRITIQUE.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="critic",
        payload=CritiqueReady(
            project_slug=payload.project_slug,
            spec_slug=result.spec_slug,
            critique_s3_key=result.critique_s3_key,
            issue_count=result.issue_count,
            high_severity_count=result.high_severity_count,
            medium_severity_count=result.medium_severity_count,
            low_severity_count=result.low_severity_count,
            summary=result.summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def upload_critique(critique: Critique, *, run_id: str) -> None:
    """Render and upload the critique Markdown to S3."""
    write_critique(run_id, render_critique(critique))


if __name__ == "__main__":
    app.run()
