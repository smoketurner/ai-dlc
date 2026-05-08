"""Strands tools the Critic uses to read the spec and write its critique.

The critic runs in the AgentCore Runtime container with IAM credentials
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
from common.memory_md import read_memory_md

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

VALID_SPEC_DOCS = frozenset({"requirements", "design", "tasks"})


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding run artifacts (specs, ADRs, critiques)."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def read_spec_doc(spec_slug: str, doc: str) -> str:
    """Read one of the three spec documents from S3.

    Args:
        spec_slug: Slug folder under ``specs/`` — e.g., ``add-healthz``.
        doc: One of ``requirements`` | ``design`` | ``tasks``.

    Returns:
        The Markdown body of the requested document.
    """
    if doc not in VALID_SPEC_DOCS:
        msg = f"doc must be one of {sorted(VALID_SPEC_DOCS)}, got {doc!r}"
        raise ValueError(msg)
    key = f"specs/{spec_slug}/{doc}.md"
    obj = s3_client().get_object(Bucket=artifacts_bucket(), Key=key)
    return obj["Body"].read().decode("utf-8")


def write_critique(run_id: str, content: str) -> str:
    """Upload the rendered critique Markdown for a run.

    Args:
        run_id: The run UUID7 string.
        content: Markdown body to upload.

    Returns:
        The full ``s3://...`` URI of the uploaded object.
    """
    bucket = artifacts_bucket()
    key = critique_s3_key(run_id)
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return f"s3://{bucket}/{key}"


def critique_s3_key(run_id: str) -> str:
    """S3 key under the artifacts bucket for a run's critique."""
    return f"runs/{run_id}/critique.md"


# Strands wrappers — added to the agent's tool list.
read_memory_md_tool = tool(read_memory_md)
read_spec_doc_tool = tool(read_spec_doc)
browse_url_tool = tool(browse_url)
