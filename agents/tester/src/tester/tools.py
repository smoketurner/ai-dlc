"""Strands tools the Tester uses to read context and post the report.

The tester runs in the AgentCore Runtime container with IAM credentials
scoped to the artifacts + memory_md S3 buckets. Tools speak directly to S3.
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING

import boto3
from strands import tool

from common.agentcore_browser import browse_url
from common.memory_md import read_stack_profile_md
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


def memory_md_bucket() -> str:
    """Bucket holding per-project MEMORY.md snapshots."""
    return os.environ["AIDLC_MEMORY_MD_BUCKET"]


def read_memory_md(project_slug: str) -> str:
    """Read the canonical MEMORY.md for a project.

    Args:
        project_slug: Project identifier — e.g., ``ai-dlc``.

    Returns:
        The Markdown body, or an empty string if no MEMORY.md exists yet.
    """
    key = f"projects/{project_slug}/MEMORY.md"
    try:
        obj = s3_client().get_object(Bucket=memory_md_bucket(), Key=key)
    except Exception:
        return ""
    return obj["Body"].read().decode("utf-8")


def read_plan_doc(plan_s3_key: str) -> str:
    """Read the architect's plan from S3.

    Args:
        plan_s3_key: Bucket-relative key — e.g., ``runs/{run_id}/plan.md``.

    Returns:
        The Markdown body of the plan, or an empty string if missing.
    """
    try:
        obj = s3_client().get_object(Bucket=artifacts_bucket(), Key=plan_s3_key)
    except Exception:
        return ""
    return obj["Body"].read().decode("utf-8")


def write_report(*, run_id: str, revision_number: int, content: str) -> str:
    """Upload the rendered test report Markdown for an impl-PR validation pass.

    Args:
        run_id: The run UUID7 string.
        revision_number: 0 for the first validation pass, 1+ after each
            implementer revision. Lets a single run accumulate multiple
            report artifacts without collision.
        content: Markdown body to upload.

    Returns:
        The full ``s3://...`` URI of the uploaded object.
    """
    bucket = artifacts_bucket()
    key = report_s3_key(run_id=run_id, revision_number=revision_number)
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return f"s3://{bucket}/{key}"


def report_s3_key(*, run_id: str, revision_number: int) -> str:
    """S3 key under the artifacts bucket for a run's test report."""
    return f"runs/{run_id}/validation/test_report-r{revision_number}.md"


# Strands wrappers — added to the agent's tool list.
read_memory_md_tool = tool(read_memory_md)
read_stack_profile_md_tool = tool(read_stack_profile_md)
read_plan_doc_tool = tool(read_plan_doc)
get_pr_diff_tool = tool(get_pr_diff)
run_pr_in_sandbox_tool = tool(run_pr_in_sandbox)
browse_url_tool = tool(browse_url)
