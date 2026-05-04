"""AgentCore Runtime entrypoint for the Reviewer.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. Step Functions
calls the runtime, which invokes the entrypoint defined here. The
entrypoint:

  1. Validates the input as :class:`ReviewerInput`.
  2. Asks the Strands agent for a :class:`Review`.
  3. Renders the review as Markdown and uploads it to S3.
  4. Posts a summary comment on the PR via ``repo_helper.comment_pr``,
     forwarding ``requestor_sub`` so the comment attributes to the
     requestor when they have linked GitHub. Falls back silently if the
     comment fails — the run shouldn't block on advisory PR commenting.
  5. Returns a :class:`ReviewerResult` for the REVIEW.READY event payload.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import ReviewerInput, ReviewerResult
from reviewer.agent import review_pr
from reviewer.review import Review, render_review, severity_counts
from reviewer.tools import write_review

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")


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
        run_id=payload.run_id,
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
    body = format_comment(review)
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


def format_comment(review: Review) -> str:
    """Render the Reviewer's PR comment body."""
    counts = severity_counts(review)
    header = (
        f"### ai-dlc reviewer — verdict: **{review.verdict}** "
        f"({counts['high']} high · {counts['medium']} medium · {counts['low']} low)"
    )
    return f"{header}\n\n{review.summary}\n"


if __name__ == "__main__":
    app.run()
