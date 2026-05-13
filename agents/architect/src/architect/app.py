"""AgentCore Runtime entrypoint for the Architect.

Validates :class:`ArchitectInput`, dispatches the agent loop on a
daemon thread (under a copied :mod:`contextvars` context — see
:func:`common.gateway_tools.fetch_gateway_token`), and returns
``{"status": "dispatched", ...}`` so the state-router gets a fast
response. The daemon clones the target repo, generates the plan via
the gateway, and emits ``DESIGN.READY`` on success or ``RUN.FAILED``
on exception.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from architect.agent import build_agent, generate_plan, model_id
from architect.plan import extract_proposed_adrs, extract_summary
from architect.repo_grounding import (
    clone_target_repo,
    sync_memory_md_from_clone,
    sync_stack_profile_from_clone,
)
from architect.tools import plan_s3_key
from common.event_emit import publish
from common.events import DesignReady, EventEnvelope, RunFailed
from common.gateway_tools import call_gateway_tool, extract_envelope, gateway_mcp_client
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ArchitectInput, ArchitectResult, usage_from_strands

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately.

    The agent body runs on a daemon thread; AgentCore keeps the
    microVM alive while ``add_async_task`` is open and tears it down
    after ``complete_async_task`` has been called and the SDK's idle
    timeout elapses.
    """
    payload = ArchitectInput.model_validate(event)
    logger.info(
        "architect invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        source_issue_url=payload.source_issue_url,
    )
    task_id = app.add_async_task("architect_run", {"run_id": payload.run_id})
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_architect, payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_architect(payload: ArchitectInput, task_id: int) -> None:
    """Body of the architect run — clones repo, generates plan, emits event.

    Runs in a daemon thread spawned from :func:`handler` under a copied
    :class:`contextvars.Context`. Always emits a terminal event
    (``DESIGN.READY`` on success, ``RUN.FAILED`` on exception) so the
    state machine advances rather than wedging. The gateway MCP client
    is held open for the duration so post-agent ``get_artifact`` reuses
    the same session.
    """
    try:
        clone_target_repo(payload.target_repo, requestor_sub=payload.requestor_sub)
        sync_memory_md_from_clone(
            project_slug=payload.project_slug,
            target_repo=payload.target_repo,
        )
        sync_stack_profile_from_clone(project_slug=payload.project_slug)
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent = build_agent(payload.run_id, mcp_client=mcp_client)
            generate_plan(
                agent,
                project_slug=payload.project_slug,
                run_id=payload.run_id,
                intent=payload.intent,
                triggering_comment_body=payload.triggering_comment_body,
                source_issue_url=payload.source_issue_url,
                source_issue_title=payload.source_issue_title,
                source_issue_body=payload.source_issue_body,
            )
            plan_body = fetch_plan_body(mcp_client, payload.run_id)
            result = ArchitectResult(
                plan_s3_key=plan_s3_key(payload.run_id),
                summary=extract_summary(plan_body),
                proposed_adrs=extract_proposed_adrs(plan_body),
                session_id=payload.run_id,
                **usage_from_strands(agent, model_id=model_id()),
            )
            logger.info(
                "plan ready",
                run_id=payload.run_id,
                plan_s3_key=result.plan_s3_key,
                proposed_adr_count=len(result.proposed_adrs),
            )
            publish_design_ready(payload, result)
    except Exception as exc:
        logger.exception("architect run failed", run_id=payload.run_id)
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(task_id)


def fetch_plan_body(mcp_client: MCPClient, run_id: str) -> str:
    """Read the architect's plan back from S3 via the gateway.

    The MCP server serializes dict tool returns into both
    ``structuredContent`` (the raw dict) and ``content[0].text`` (a
    JSON string of the same dict). We prefer the structured form and
    fall back to parsing the text block so the helper is robust to
    servers that haven't enabled structured output.
    """
    result = call_gateway_tool(
        mcp_client,
        name="artifact_tool",
        arguments={"op": "get_artifact", "key": plan_s3_key(run_id)},
    )
    envelope = extract_envelope(result)
    if not envelope.get("ok"):
        msg = f"artifact_tool.get_artifact returned an error envelope: {envelope!r}"
        raise RuntimeError(msg)
    inner = envelope.get("result")
    if not isinstance(inner, dict) or "content" not in inner:
        msg = f"artifact_tool.get_artifact envelope missing result.content: {envelope!r}"
        raise RuntimeError(msg)
    return str(inner["content"])


def publish_design_ready(payload: ArchitectInput, result: ArchitectResult) -> None:
    """Emit DESIGN.READY so the projector advances the run to ``designed``."""
    envelope = EventEnvelope[DesignReady](
        event_id=new_event_id(),
        type="DESIGN.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="architect",
        payload=DesignReady(
            project_slug=payload.project_slug,
            plan_s3_key=result.plan_s3_key,
            summary=result.summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


def publish_run_failed(payload: ArchitectInput, exc: BaseException) -> None:
    """Emit RUN.FAILED so the projector terminates the run on agent crash.

    Without this, a thrown exception inside the background thread would
    leave the run wedged in ``architect_running`` with nothing else to
    advance it — the state-router has already reported a successful
    dispatch.
    """
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="architect",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="architect_running",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
