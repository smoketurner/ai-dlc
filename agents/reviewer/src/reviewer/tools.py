"""Strands tools the Reviewer uses to read context and post review artifacts.

The reviewer runs in the AgentCore Runtime container with IAM credentials
scoped to the artifacts + memory_md S3 buckets. Tools speak directly to S3.

Each operation has a plain Python function plus a Strands ``@tool`` wrapper
with a ``_tool`` suffix.
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING

import boto3
from strands import tool

from common.agentcore_browser import browse_url
from common.memory_md import read_memory_md, read_stack_profile_md
from common.sandbox import get_pr_diff, run_pr_in_sandbox

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding run artifacts."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def read_plan_doc(plan_s3_key: str) -> str:
    """Read the architect's plan from S3.

    Args:
        plan_s3_key: Bucket-relative key — e.g., ``runs/{run_id}/plan.md``.

    Returns:
        The Markdown body of the plan, or an empty string if the key is
        missing (the reviewer should still run; it just lacks the plan
        context the architect produced).
    """
    try:
        obj = s3_client().get_object(Bucket=artifacts_bucket(), Key=plan_s3_key)
    except Exception:
        return ""
    return obj["Body"].read().decode("utf-8")


def write_review(*, run_id: str, revision_number: int, content: str) -> str:
    """Upload the rendered review Markdown for an impl-PR validation pass.

    Args:
        run_id: The run UUID7 string.
        revision_number: 0 for the first validation pass, 1+ after each
            implementer revision. Lets a single run accumulate multiple
            review artifacts without collision.
        content: Markdown body to upload.

    Returns:
        The full ``s3://...`` URI of the uploaded object.
    """
    bucket = artifacts_bucket()
    key = review_s3_key(run_id=run_id, revision_number=revision_number)
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return f"s3://{bucket}/{key}"


def review_s3_key(*, run_id: str, revision_number: int) -> str:
    """S3 key under the artifacts bucket for a run's review artifact."""
    return f"runs/{run_id}/validation/review-r{revision_number}.md"


# Strands wrappers — added to the agent's tool list.
read_memory_md_tool = tool(read_memory_md)
read_stack_profile_md_tool = tool(read_stack_profile_md)
read_plan_doc_tool = tool(read_plan_doc)
get_pr_diff_tool = tool(get_pr_diff)
run_pr_in_sandbox_tool = tool(run_pr_in_sandbox)
browse_url_tool = tool(browse_url)
