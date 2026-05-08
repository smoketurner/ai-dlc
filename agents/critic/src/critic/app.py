"""AgentCore Runtime entrypoint for the Critic.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The state-router
Lambda invokes this runtime when a run reaches ``critic_running``. The
entrypoint:

  1. Validates the input as :class:`CriticInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the work runs.
  3. Spawns a daemon thread that critiques the spec, uploads the
     critique to S3, emits ``CRITIQUE.READY``, and acknowledges the
     async task.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms so the state-router sees a clean fast response and
     AgentCore's frontend doesn't retry the dispatch.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import CritiqueReady, EventEnvelope, RunFailed
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import CriticInput, CriticResult, usage_from_strands
from critic.agent import build_agent, critique_spec, model_id
from critic.critique import Critique, render_critique, severity_counts
from critic.tools import critique_s3_key, write_critique

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = CriticInput.model_validate(event)
    logger.info(
        "critic invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        spec_slug=payload.spec_slug,
    )
    task_id = app.add_async_task("critic_run", {"run_id": payload.run_id})
    threading.Thread(
        target=run_critic,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_critic(payload: CriticInput, task_id: int) -> None:
    """Body of the critic run — generates critique, emits event.

    Runs in a daemon thread spawned from :func:`handler`. Always emits
    a terminal event so the state machine advances rather than wedging.
    """
    try:
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
    except Exception as exc:
        logger.exception("critic run failed", run_id=payload.run_id)
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(task_id)


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


def publish_run_failed(payload: CriticInput, exc: BaseException) -> None:
    """Emit RUN.FAILED so the projector terminates the run on agent crash."""
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="critic",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="critic_running",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


def upload_critique(critique: Critique, *, run_id: str) -> None:
    """Render and upload the critique Markdown to S3."""
    write_critique(run_id, render_critique(critique))


if __name__ == "__main__":
    app.run()
