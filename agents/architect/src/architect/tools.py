"""Strands tools the Architect uses to read MEMORY.md and write spec docs.

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

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

_VALID_DOCS = frozenset({"requirements", "design", "tasks"})


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding run artifacts (specs, ADRs, code diffs)."""
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


def write_spec_doc(spec_slug: str, doc: str, content: str) -> str:
    """Write one of the three spec documents to S3.

    Args:
        spec_slug: Slug folder under ``specs/`` — e.g., ``add-healthz``.
        doc: One of ``requirements`` | ``design`` | ``tasks``.
        content: Markdown body to upload.

    Returns:
        The full ``s3://...`` URI of the uploaded object.
    """
    if doc not in _VALID_DOCS:
        msg = f"doc must be one of {sorted(_VALID_DOCS)}, got {doc!r}"
        raise ValueError(msg)
    bucket = artifacts_bucket()
    key = f"specs/{spec_slug}/{doc}.md"
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return f"s3://{bucket}/{key}"


# Strands wrappers — added to the agent's tool list. Each wraps the plain
# function above so the agent can call it through the LLM tool-use protocol.
read_memory_md_tool = tool(read_memory_md)
write_spec_doc_tool = tool(write_spec_doc)
