"""AgentCore Runtime entrypoint for the Critic.

Validates :class:`CriticInput`, dispatches the agent loop on a daemon
thread (under a copied :mod:`contextvars` context — see
:func:`common.gateway_tools.fetch_gateway_token`), and returns
``{"status": "dispatched", ...}`` so the state-router gets a fast
response. The daemon critiques the architect's plan, uploads the
critique via the gateway, and emits ``CRITIQUE.READY`` on success or
``RUN.FAILED`` on exception.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from common.event_emit import publish
from common.events import CritiqueReady, EventEnvelope, RunFailed
from common.gateway_tools import ARTIFACT_TOOL, call_gateway_tool, gateway_mcp_client
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import (
    CriticInput,
    CriticResult,
    invoke_with_fallback,
    usage_from_strands,
)
from critic.agent import build_agent, critique_plan, fallback_model_id, model_id
from critic.critique import Critique, render_critique, severity_counts
from critic.tools import critique_s3_key

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, dispatch the run on a daemon thread, return fast."""
    payload = CriticInput.model_validate(event)
    logger.info(
        "critic invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        plan_s3_key=payload.plan_s3_key,
    )
    task_id = app.add_async_task("critic_run", {"run_id": payload.run_id})
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_critic, payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_critic(payload: CriticInput, task_id: int) -> None:
    """Body of the critic run — generates critique, uploads it, emits event.

    Runs in a daemon thread spawned from :func:`handler` under a copied
    :class:`contextvars.Context`. Always emits a terminal event so the
    state machine advances rather than wedging. The gateway MCP client
    is held open for the duration so post-agent ``put_artifact`` reuses
    the same session.
    """
    try:
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent, used_model_id, critique = invoke_with_fallback(
                primary_model_id=model_id(),
                fallback_model_id=fallback_model_id(),
                build=lambda m: build_agent(
                    payload.run_id, mcp_client=mcp_client, model_id_override=m
                ),
                run=lambda a: critique_plan(
                    a,
                    project_slug=payload.project_slug,
                    run_id=payload.run_id,
                    plan_s3_key=payload.plan_s3_key,
                    intent=payload.intent,
                    source_issue_url=payload.source_issue_url,
                    source_issue_title=payload.source_issue_title,
                    source_issue_body=payload.source_issue_body,
                ),
            )
            upload_critique(mcp_client, critique, run_id=payload.run_id)

            counts = severity_counts(critique)
            result = CriticResult(
                critique_s3_key=critique_s3_key(payload.run_id),
                issue_count=len(critique.issues),
                high_severity_count=counts["high"],
                medium_severity_count=counts["medium"],
                low_severity_count=counts["low"],
                summary=critique.summary[:2048],
                session_id=payload.run_id,
                **usage_from_strands(agent, model_id=used_model_id),
            )
            logger.info(
                "critique ready",
                run_id=payload.run_id,
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
    """Emit CRITIQUE.READY so the projector advances the run to ``critiqued``."""
    envelope = EventEnvelope[CritiqueReady](
        event_id=new_event_id(),
        type="CRITIQUE.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="critic",
        payload=CritiqueReady(
            project_slug=payload.project_slug,
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


def upload_critique(mcp_client: MCPClient, critique: Critique, *, run_id: str) -> None:
    """Render the critique and upload via the artifact_tool gateway target."""
    call_gateway_tool(
        mcp_client,
        name=ARTIFACT_TOOL,
        arguments={
            "op": "put_artifact",
            "key": critique_s3_key(run_id),
            "content": render_critique(critique),
        },
    )


if __name__ == "__main__":
    app.run()
