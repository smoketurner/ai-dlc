"""AgentCore Runtime entrypoint for the Code-Critic.

The state-router invokes this runtime when a run reaches
``impl_pr_open`` (in parallel with reviewer + tester) for a validation
pass against the integrated impl PR.

  1. Validates the input as :class:`CodeCriticInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the work runs.
  3. Spawns a daemon thread under a copied :class:`contextvars.Context`
     that critiques the diff against the **original GitHub issue**,
     uploads the critique via the per-agent gateway, posts a comment
     on the impl PR via the same gateway, emits
     ``CODE_CRITIQUE.READY``, and acknowledges the async task.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in ~100ms.

``contextvars.copy_context()`` carries the runtime's
``WorkloadAccessToken`` ContextVar into the daemon thread so
:func:`common.gateway_tools.fetch_gateway_token` can exchange it for a
Cognito M2M JWT via AgentCore Identity.
"""

from __future__ import annotations

import contextvars
import re
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from code_critic.agent import build_agent, critique_pr, model_id
from code_critic.critique import Critique, render_critique, severity_counts
from code_critic.tools import critique_s3_key
from common.event_emit import publish
from common.events import CodeCritiqueReady, EventEnvelope, RunFailed
from common.gateway_tools import call_gateway_tool, gateway_mcp_client
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import CodeCriticInput, CodeCriticResult, usage_from_strands

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = CodeCriticInput.model_validate(event)
    logger.info(
        "code-critic invoked",
        run_id=payload.run_id,
        pr_url=payload.pr_url,
        revision_number=payload.revision_number,
        source_issue_url=payload.source_issue_url,
    )
    async_task_id = app.add_async_task("code_critic_run", {"run_id": payload.run_id})
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_code_critic, payload, async_task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "async_task_id": async_task_id}


def run_code_critic(payload: CodeCriticInput, async_task_id: int) -> None:
    """Body of the code-critic run — adversarial review of the integrated diff."""
    try:
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent = build_agent(payload.run_id, mcp_client=mcp_client)
            critique = critique_pr(
                agent,
                project_slug=payload.project_slug,
                plan_s3_key=payload.plan_s3_key,
                run_id=payload.run_id,
                pr_url=payload.pr_url,
                revision_number=payload.revision_number,
                source_issue_url=payload.source_issue_url,
                source_issue_title=payload.source_issue_title,
                source_issue_body=payload.source_issue_body,
            )
            upload_critique(
                mcp_client,
                critique,
                run_id=payload.run_id,
                revision_number=payload.revision_number,
            )
            post_pr_comment(mcp_client, payload=payload, critique=critique)

            counts = severity_counts(critique)
            result = CodeCriticResult(
                pr_url=payload.pr_url,
                critique_s3_key=critique_s3_key(
                    run_id=payload.run_id,
                    revision_number=payload.revision_number,
                ),
                issue_count=len(critique.issues),
                high_severity_count=counts["high"],
                medium_severity_count=counts["medium"],
                low_severity_count=counts["low"],
                summary=critique.summary[:2048],
                session_id=f"{payload.run_id}-code-critic-r{payload.revision_number}",
                **usage_from_strands(agent, model_id=model_id()),
            )
            logger.info(
                "code critique ready",
                run_id=payload.run_id,
                issue_count=result.issue_count,
                high=result.high_severity_count,
            )
            publish_code_critique_ready(payload, result)
    except Exception as exc:
        logger.exception("code-critic run failed", run_id=payload.run_id)
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(async_task_id)


def publish_code_critique_ready(
    payload: CodeCriticInput,
    result: CodeCriticResult,
) -> None:
    """Emit CODE_CRITIQUE.READY — advisory, does not advance state."""
    envelope = EventEnvelope[CodeCritiqueReady](
        event_id=new_event_id(),
        type="CODE_CRITIQUE.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="code_critic",
        payload=CodeCritiqueReady(
            project_slug=payload.project_slug,
            pr_url=result.pr_url,
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


def publish_run_failed(payload: CodeCriticInput, exc: BaseException) -> None:
    """Emit RUN.FAILED so the projector terminates the run on agent crash."""
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="code_critic",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="validation_running",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


def upload_critique(
    mcp_client: MCPClient,
    critique: Critique,
    *,
    run_id: str,
    revision_number: int,
) -> None:
    """Render and upload the critique Markdown via the artifact_tool gateway target."""
    call_gateway_tool(
        mcp_client,
        name="artifact_tool",
        arguments={
            "op": "put_artifact",
            "key": critique_s3_key(run_id=run_id, revision_number=revision_number),
            "content": render_critique(critique),
        },
    )


def post_pr_comment(
    mcp_client: MCPClient,
    *,
    payload: CodeCriticInput,
    critique: Critique,
) -> None:
    """Best-effort summary comment on the impl PR via the repo_helper gateway target."""
    parsed = PR_URL_PATTERN.match(payload.pr_url)
    if parsed is None:
        logger.warning("could not parse pr_url for comment", pr_url=payload.pr_url)
        return
    body = render_critique(critique)
    try:
        call_gateway_tool(
            mcp_client,
            name="repo_helper",
            arguments={
                "op": "comment_pr",
                "repo": parsed.group("repo"),
                "pr_number": int(parsed.group("num")),
                "body": body,
                "requestor_sub": payload.requestor_sub,
            },
        )
    except Exception as exc:
        logger.warning("comment_pr failed", err=str(exc), pr_url=payload.pr_url)


if __name__ == "__main__":
    app.run()
