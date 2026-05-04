"""AgentCore Runtime entrypoint for the Reviewer.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. Step Functions
calls the runtime, which invokes the entrypoint defined here. The
entrypoint:

  1. Validates the input as :class:`ReviewerInput`.
  2. Asks the Strands agent for a :class:`Review`.
  3. Renders the review as Markdown and uploads it to S3.
  4. Returns a :class:`ReviewerResult` for the REVIEW.READY event payload.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import ReviewerInput, ReviewerResult
from reviewer.agent import review_pr
from reviewer.review import Review, render_review, severity_counts
from reviewer.tools import write_review

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Reviewer entrypoint. Returns a JSON-serialisable ReviewerResult."""
    payload = ReviewerInput.model_validate(event)
    logger.info(
        "reviewer invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        pr_url=payload.pr_url,
    )

    review = review_pr(
        project_slug=payload.project_slug,
        spec_slug=payload.spec_slug,
        task_id=payload.task_id,
        pr_url=payload.pr_url,
        diff_summary=payload.diff_summary,
    )
    upload_review(review, run_id=payload.run_id, task_id=payload.task_id)

    counts = severity_counts(review)
    result = ReviewerResult(
        task_id=review.task_id,
        pr_url=payload.pr_url,
        verdict=review.verdict,
        comment_count=len(review.comments),
        high_severity_count=counts["high"],
        medium_severity_count=counts["medium"],
        low_severity_count=counts["low"],
        summary=review.summary[:2048],
        session_id=f"{payload.run_id}-{payload.task_id}-reviewer",
    )
    logger.info(
        "review ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
        verdict=result.verdict,
        comment_count=result.comment_count,
    )
    return result.model_dump()


def upload_review(review: Review, *, run_id: str, task_id: str) -> None:
    """Render and upload the review Markdown to S3."""
    write_review(run_id=run_id, task_id=task_id, content=render_review(review))


if __name__ == "__main__":
    app.run()
