"""Strands tools the Retrospector uses to gather context.

The Retrospector reads:

  * the closed PR + its issue-style comments + its inline review-thread
    comments, when the terminal event was a PR close (merged or not);
  * the closed issue + its comment thread, when the terminal event was
    an issue close / bot unassign;
  * the project's ``MEMORY.md`` (via the artifact_tool gateway, same
    as architect/proposer) so it can avoid proposing duplicates;
  * validator artifacts in S3 (``runs/{run_id}/validation/*.md``) when
    the terminal event was a cap-hit ``RUN.FAILED`` — the dispatcher
    enumerates the per-round keys and the agent reads each round's
    reviewer / tester / code-critic findings to spot the recurring
    pattern the implementer never fixed.

All GitHub reads go through the existing ``repo_helper`` Lambda — no
direct GitHub credentials in this container.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from strands import tool

from common.memory_md import read_memory_md, read_stack_profile_md

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client (used for repo_helper invocation)."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str:
    """Function name of the repo_helper Lambda — same env var ``app.py`` reads."""
    return os.environ["AIDLC_REPO_HELPER_FUNCTION_NAME"]


def invoke_repo_helper(op: str, **fields: Any) -> dict[str, Any]:
    """Invoke ``repo_helper`` with one op; return the standard envelope."""
    response = lambda_client().invoke(
        FunctionName=repo_helper_function_name(),
        InvocationType="RequestResponse",
        Payload=json.dumps({"input": {"op": op, **fields}}).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if not body.get("ok"):
        return {"error": body.get("error")}
    return {"result": body.get("result", {})}


def get_pr(repo: str, pr_number: int) -> dict[str, Any]:
    """Read a PR's title, body, state, merged flag, and head SHA."""
    return invoke_repo_helper("get_pr", repo=repo, pr_number=pr_number)


def list_pr_comments(repo: str, pr_number: int) -> dict[str, Any]:
    """List the issue-style conversation comments on a PR."""
    return invoke_repo_helper("list_pr_comments", repo=repo, pr_number=pr_number)


def list_pr_review_comments(repo: str, pr_number: int) -> dict[str, Any]:
    """List the inline review-thread (line-anchored) comments on a PR."""
    return invoke_repo_helper("list_pr_review_comments", repo=repo, pr_number=pr_number)


def get_issue(repo: str, issue_number: int) -> dict[str, Any]:
    """Read an issue's title, body, labels, state."""
    return invoke_repo_helper("get_issue", repo=repo, issue_number=issue_number)


def list_issue_comments(repo: str, issue_number: int) -> dict[str, Any]:
    """List comments on an issue in chronological order."""
    return invoke_repo_helper("list_issue_comments", repo=repo, issue_number=issue_number)


@cache
def s3_client() -> S3Client:
    """Process-cached S3 client for the artifacts bucket."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding run artifacts (validator markdown, plan.md, etc.)."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def read_validation_artifact(key: str) -> str:
    """Read one validator artifact from the artifacts bucket.

    On cap-hit ``RUN.FAILED`` events the dispatcher enumerates the keys
    of every reviewer / tester / code-critic markdown produced across
    all revision rounds (``runs/{run_id}/validation/{kind}-r{N}.md``).
    The retrospector reads each key with this tool and looks for the
    finding that recurs round after round — that's the lesson worth
    persisting.

    Args:
        key: Bucket-relative S3 key (no leading slash).

    Returns:
        The Markdown body, or the empty string if the key is missing.
    """
    try:
        obj = s3_client().get_object(Bucket=artifacts_bucket(), Key=key)
    except Exception:
        return ""
    return obj["Body"].read().decode("utf-8")


# Strands wrappers — exposed to the agent.
read_memory_md_tool = tool(read_memory_md)
read_stack_profile_md_tool = tool(read_stack_profile_md)
get_pr_tool = tool(get_pr)
list_pr_comments_tool = tool(list_pr_comments)
list_pr_review_comments_tool = tool(list_pr_review_comments)
get_issue_tool = tool(get_issue)
list_issue_comments_tool = tool(list_issue_comments)
read_validation_artifact_tool = tool(read_validation_artifact)
