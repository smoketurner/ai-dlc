"""Strands tools the Proposer uses for issue-driven research runs.

The Proposer reads MEMORY.md, browses external best-practice docs via
an AgentCore browser session, and reads issue comment threads via the
repo_helper Lambda when the user is asking about prior research output.
It never reads or writes the SDLC pipeline state directly.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from strands import tool

from common.agentcore_browser import browse_url
from common.memory_md import read_memory_md

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient


def artifacts_bucket() -> str:
    """Bucket holding project artifacts (currently used for MEMORY.md reads)."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client (used for repo_helper invocation)."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str:
    """Function name of the repo_helper Lambda — same env var ``app.py`` reads."""
    return os.environ["AIDLC_REPO_HELPER_FUNCTION_NAME"]


def list_issue_comments(repo: str, issue_number: int) -> dict[str, Any]:
    """List comments on an issue in chronological order.

    Used by the proposer to read its own prior synthesis comment + any
    follow-up reply when the user is asking for issue spawning based on
    earlier research output.

    Args:
        repo: ``owner/name`` of the GitHub repository.
        issue_number: Issue number to read comments from.

    Returns:
        A dict with ``comments`` — a list of ``{id, user, user_type,
        body, created_at, updated_at, html_url}`` entries — or
        ``{"error": ...}`` if the repo_helper Lambda returned a failure
        envelope.
    """
    response = lambda_client().invoke(
        FunctionName=repo_helper_function_name(),
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {
                "input": {
                    "op": "list_issue_comments",
                    "repo": repo,
                    "issue_number": issue_number,
                },
            },
        ).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if not body.get("ok"):
        return {"error": body.get("error")}
    result = body.get("result") or {}
    return {"comments": result.get("comments", [])}


# Strands wrappers — exposed to the agent.
read_memory_md_tool = tool(read_memory_md)
browse_url_tool = tool(browse_url)
list_issue_comments_tool = tool(list_issue_comments)
