"""AgentCore Runtime entrypoint for the Reviewer.

The state-router invokes the runtime once per task PR. The entrypoint:

  1. Validates the input as :class:`ReviewerInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the review runs.
  3. Spawns a daemon thread that runs the Strands agent, uploads the
     review to S3, posts a summary comment on the PR, and emits
     ``REVIEW.READY``. On exception the thread logs and acknowledges
     the async task — reviewer is advisory, so a crash doesn't
     advance any state machine.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, ReviewReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ReviewerInput, ReviewerResult, usage_from_strands
from reviewer.agent import build_agent, model_id, review_pr
from reviewer.review import Review, ReviewSummary, render_review, severity_counts
from reviewer.tools import write_review

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

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
        task_id=payload.task_id,
        pr_url=payload.pr_url,
    )
    task_id = app.add_async_task(
        "reviewer_run",
        {"run_id": payload.run_id, "task_id": payload.task_id},
    )
    threading.Thread(
        target=run_reviewer,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {
        "status": "dispatched",
        "run_id": payload.run_id,
        "task_id": payload.task_id,
        "async_task_id": task_id,
    }


def run_reviewer(payload: ReviewerInput, async_task_id: int) -> None:
    """Body of the reviewer run — produces review, posts comment, emits event.

    Reviewer is advisory; an exception is logged and swallowed. The
    run continues toward human approval through the normal PR-comment
    UX without a Reviewer summary.
    """
    try:
        agent = build_agent(payload.run_id)
        review = review_pr(
            agent,
            project_slug=payload.project_slug,
            spec_slug=payload.spec_slug,
            task_id=payload.task_id,
            pr_url=payload.pr_url,
            diff_summary=payload.diff_summary,
        )
        upload_review(review, run_id=payload.run_id, task_id=payload.task_id)
        post_pr_comment(payload=payload, review=review)

        counts = severity_counts(review)
        result = ReviewerResult(
            task_id=review.task_id,
            pr_url=payload.pr_url,
            verdict=review.verdict,
            comment_count=len(review.comments),
            high_severity_count=counts["high"],
            medium_severity_count=counts["medium"],
            low_severity_count=counts["low"],
            summary=event_summary(review.summary),
            session_id=f"{payload.run_id}-{payload.task_id}-reviewer",
            **usage_from_strands(agent, model_id=model_id()),
        )
        logger.info(
            "review ready",
            run_id=payload.run_id,
            task_id=payload.task_id,
            verdict=result.verdict,
            comment_count=result.comment_count,
        )
        publish_review_ready(payload, result)
    except Exception:
        logger.exception(
            "reviewer run failed",
            run_id=payload.run_id,
            task_id=payload.task_id,
        )
    finally:
        app.complete_async_task(async_task_id)


def upload_review(review: Review, *, run_id: str, task_id: str) -> None:
    """Render and upload the review Markdown to S3."""
    write_review(run_id=run_id, task_id=task_id, content=render_review(review))


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
            spec_slug=payload.spec_slug,
            task_id=result.task_id,
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


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client for invoking ``repo_helper``."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str | None:
    """Lambda function name for ``repo_helper`` — empty when not wired in this env."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME") or None


def post_pr_comment(*, payload: ReviewerInput, review: Review) -> None:
    """Best-effort summary comment on the PR. Never raises — advisory only."""
    fn = repo_helper_function_name()
    if fn is None:
        return
    parsed = PR_URL_PATTERN.match(payload.pr_url)
    if parsed is None:
        logger.warning("could not parse pr_url for comment", pr_url=payload.pr_url)
        return
    body = render_review(review)
    try:
        lambda_client().invoke(
            FunctionName=fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {
                    "input": {
                        "op": "comment_pr",
                        "repo": parsed.group("repo"),
                        "pr_number": int(parsed.group("num")),
                        "body": body,
                        "requestor_sub": payload.requestor_sub,
                    },
                },
            ).encode("utf-8"),
        )
    except Exception as exc:
        logger.warning("comment_pr failed", err=str(exc), pr_url=payload.pr_url)


if __name__ == "__main__":
    app.run()
