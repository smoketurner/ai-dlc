"""Strands tools the Architect uses to read context and write the plan.

The architect runs in the AgentCore Runtime container, which has IAM
credentials scoped to the artifacts + memory_md S3 buckets. Tools
speak directly to S3.

Each operation has a plain Python function (callable from anywhere, including
``app.py`` for deterministic post-agent uploads) plus a Strands ``@tool``
wrapper of the same name with a ``_tool`` suffix that is added to the agent's
toolset.
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING

import boto3
from strands import tool

from architect.repo_grounding import list_repo_paths, read_repo_file
from common.agentcore_browser import browse_url
from common.memory_md import read_memory_md, read_stack_profile_md

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding run artifacts (plans, critiques, validation reports)."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def plan_s3_key(run_id: str) -> str:
    """S3 key under the artifacts bucket for a run's plan document."""
    return f"runs/{run_id}/plan.md"


def write_plan_doc(run_id: str, content: str) -> str:
    """Write the architect's plan markdown to ``runs/{run_id}/plan.md``.

    Args:
        run_id: The run UUID7 string.
        content: Markdown body to upload — the full plan document.

    Returns:
        The full ``s3://...`` URI of the uploaded object.
    """
    bucket = artifacts_bucket()
    key = plan_s3_key(run_id)
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return f"s3://{bucket}/{key}"


def read_plan_doc(run_id: str) -> str:
    """Read a previously-written plan body back from S3 (best-effort)."""
    bucket = artifacts_bucket()
    key = plan_s3_key(run_id)
    obj = s3_client().get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


# Strands wrappers — added to the agent's tool list. Each wraps the plain
# function above so the agent can call it through the LLM tool-use protocol.
read_memory_md_tool = tool(read_memory_md)
read_stack_profile_md_tool = tool(read_stack_profile_md)
write_plan_doc_tool = tool(write_plan_doc)
list_repo_paths_tool = tool(list_repo_paths)
read_repo_file_tool = tool(read_repo_file)
browse_url_tool = tool(browse_url)
