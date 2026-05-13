"""AgentCore Runtime entrypoint for the Reviewer.

Validates :class:`ReviewerInput`, dispatches the agent loop on a
daemon thread (under a copied :mod:`contextvars` context — see
:func:`common.gateway_tools.fetch_gateway_token`), and returns
``{"status": "dispatched", ...}`` so the state-router gets a fast
response. The daemon runs the agent, uploads the review via the
gateway, posts a summary comment on the PR, and emits
``REVIEW.READY``. The reviewer's verdict gates the run; on uncaught
exception the daemon logs and acknowledges the async task — the run
stays in ``validation_running`` waiting on the other validators rather
than wedging on a missing reviewer pass.
"""

from __future__ import annotations

import contextvars
import re
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from common.event_emit import publish
from common.events import EventEnvelope, ReviewReady
from common.gateway_tools import call_gateway_tool, gateway_mcp_client
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ReviewerInput, ReviewerResult, usage_from_strands
from reviewer.agent import build_agent, model_id, review_pr
from reviewer.review import Review, ReviewSummary, render_review, severity_counts
from reviewer.tools import review_s3_key

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = ReviewerInput.model_validate(event)
    logger.info(
        "reviewer invoked",
        run_id=payload.run_id,
        pr_url=payload.pr_url,
        revision_number=payload.revision_number,
    )
    async_task_id = app.add_async_task(
        "reviewer_run",
        {"run_id": payload.run_id, "revision_number": payload.revision_number},
    )
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_reviewer, payload, async_task_id),
        daemon=True,
    ).start()
    return {
        "status": "dispatched",
        "run_id": payload.run_id,
        "async_task_id": async_task_id,
    }


def run_reviewer(payload: ReviewerInput, async_task_id: int) -> None:
    """Body of the reviewer run — produces review, posts comment, emits event."""
    try:
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent = build_agent(payload.run_id, mcp_client=mcp_client)
            review = review_pr(
                agent,
                project_slug=payload.project_slug,
                plan_s3_key=payload.plan_s3_key,
                run_id=payload.run_id,
                pr_url=payload.pr_url,
                revision_number=payload.revision_number,
            )
            upload_review(
                mcp_client,
                review,
                run_id=payload.run_id,
                revision_number=payload.revision_number,
            )
            post_pr_comment(mcp_client, payload=payload, review=review)

            counts = severity_counts(review)
            result = ReviewerResult(
                pr_url=payload.pr_url,
                verdict=review.verdict,
                comment_count=len(review.comments),
                high_severity_count=counts["high"],
                medium_severity_count=counts["medium"],
                low_severity_count=counts["low"],
                summary=event_summary(review.summary),
                session_id=f"{payload.run_id}-reviewer-r{payload.revision_number}",
                **usage_from_strands(agent, model_id=model_id()),
            )
            logger.info(
                "review ready",
                run_id=payload.run_id,
                verdict=result.verdict,
                comment_count=result.comment_count,
            )
            publish_review_ready(payload, result)
    except Exception:
        logger.exception(
            "reviewer run failed",
            run_id=payload.run_id,
        )
    finally:
        app.complete_async_task(async_task_id)


def upload_review(
    mcp_client: MCPClient,
    review: Review,
    *,
    run_id: str,
    revision_number: int,
) -> None:
    """Render and upload the review Markdown via the artifact_tool gateway target."""
    call_gateway_tool(
        mcp_client,
        name="artifact_tool",
        arguments={
            "op": "put_artifact",
            "key": review_s3_key(run_id=run_id, revision_number=revision_number),
            "content": render_review(review),
        },
    )


def event_summary(summary: ReviewSummary) -> str:
    """Flatten the structured summary to a single string for the REVIEW.READY event."""
    parts = [f"Context: {summary.context}"]
    if summary.issue:
        parts.append(f"Issue: {summary.issue}")
    if summary.actual_vs_expected:
        parts.append(f"Actual vs. expected: {summary.actual_vs_expected}")
    parts.append(f"Impact: {summary.impact}")
    return " | ".join(parts)[:2048]


def publish_review_ready(payload: ReviewerInput, result: ReviewerResult) -> None:
    """Emit REVIEW.READY for the projector + dashboard."""
    envelope = EventEnvelope[ReviewReady](
        event_id=new_event_id(),
        type="REVIEW.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="reviewer",
        payload=ReviewReady(
            project_slug=payload.project_slug,
            pr_url=result.pr_url,
            verdict=result.verdict,
            comment_count=result.comment_count,
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


def post_pr_comment(
    mcp_client: MCPClient,
    *,
    payload: ReviewerInput,
    review: Review,
) -> None:
    """Best-effort summary comment on the PR via the repo_helper gateway target."""
    parsed = PR_URL_PATTERN.match(payload.pr_url)
    if parsed is None:
        logger.warning("could not parse pr_url for comment", pr_url=payload.pr_url)
        return
    body = render_review(review)
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
