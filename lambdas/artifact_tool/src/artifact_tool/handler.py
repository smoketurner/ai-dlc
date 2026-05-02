"""AgentCore Gateway target Lambda for artifact + MEMORY.md operations.

The Gateway invokes this Lambda for each MCP `invokeTool` call. The event
shape is the standard AgentCore Gateway → Lambda payload: an envelope with
the tool name, the structured input, and request context. We dispatch on
the ``op`` field of the input to one of: ``put_artifact``, ``get_artifact``,
``list_artifacts``, ``read_memory_md``, ``write_memory_md``.

The Lambda is intentionally thin — it owns the S3 contract for the artifacts
and memory_md buckets, nothing else. Bucket names come from environment
variables set by the Terraform module that owns this function.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


logger = Logger(service="artifact_tool")


@cache
def s3() -> S3Client:
    """Return a process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Return the run-artifacts bucket name from the env."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def memory_md_bucket() -> str:
    """Return the MEMORY.md bucket name from the env."""
    return os.environ["AIDLC_MEMORY_MD_BUCKET"]


class BaseOp(BaseModel):
    """Common configuration for every input model."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PutArtifactInput(BaseOp):
    """Write a UTF-8 text artifact to the artifacts bucket."""

    op: Literal["put_artifact"]
    key: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=5_000_000)


class GetArtifactInput(BaseOp):
    """Read a UTF-8 text artifact from the artifacts bucket."""

    op: Literal["get_artifact"]
    key: str = Field(min_length=1, max_length=1024)


class ListArtifactsInput(BaseOp):
    """List artifact keys under a prefix."""

    op: Literal["list_artifacts"]
    prefix: str = Field(default="", max_length=1024)
    max_keys: int = Field(default=100, ge=1, le=1000)


class ReadMemoryMdInput(BaseOp):
    """Read the latest MEMORY.md snapshot for a project."""

    op: Literal["read_memory_md"]
    project_slug: str = Field(min_length=1, max_length=64)


class WriteMemoryMdInput(BaseOp):
    """Write a MEMORY.md snapshot for a project."""

    op: Literal["write_memory_md"]
    project_slug: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=2_000_000)


def put_text(bucket: str, key: str, content: str) -> None:
    """UTF-8 PUT to ``bucket``/``key`` (bucket default SSE applies)."""
    s3().put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def put_artifact(req: PutArtifactInput) -> dict[str, Any]:
    """Write a UTF-8 text artifact to the artifacts bucket."""
    bucket = artifacts_bucket()
    put_text(bucket, req.key, req.content)
    return {"bucket": bucket, "key": req.key}


def get_artifact(req: GetArtifactInput) -> dict[str, Any]:
    """Read a UTF-8 text artifact from the artifacts bucket."""
    obj = s3().get_object(Bucket=artifacts_bucket(), Key=req.key)
    return {"key": req.key, "content": obj["Body"].read().decode("utf-8")}


def list_artifacts(req: ListArtifactsInput) -> dict[str, Any]:
    """List artifact keys in the artifacts bucket under ``req.prefix``."""
    resp = s3().list_objects_v2(
        Bucket=artifacts_bucket(),
        Prefix=req.prefix,
        MaxKeys=req.max_keys,
    )
    keys = [item["Key"] for item in resp.get("Contents", [])]
    return {"prefix": req.prefix, "keys": keys}


def read_memory_md(req: ReadMemoryMdInput) -> dict[str, Any]:
    """Read the canonical ``MEMORY.md`` for a project."""
    key = f"projects/{req.project_slug}/MEMORY.md"
    obj = s3().get_object(Bucket=memory_md_bucket(), Key=key)
    return {"project_slug": req.project_slug, "content": obj["Body"].read().decode("utf-8")}


def write_memory_md(req: WriteMemoryMdInput) -> dict[str, Any]:
    """Update both the canonical and the per-session ``MEMORY.md`` for a project."""
    bucket = memory_md_bucket()
    canonical_key = f"projects/{req.project_slug}/MEMORY.md"
    snapshot_key = f"projects/{req.project_slug}/sessions/{req.session_id}/MEMORY.md"
    put_text(bucket, canonical_key, req.content)
    put_text(bucket, snapshot_key, req.content)
    return {
        "project_slug": req.project_slug,
        "canonical_key": canonical_key,
        "snapshot_key": snapshot_key,
    }


DISPATCH: dict[str, tuple[type[BaseOp], Any]] = {
    "put_artifact": (PutArtifactInput, put_artifact),
    "get_artifact": (GetArtifactInput, get_artifact),
    "list_artifacts": (ListArtifactsInput, list_artifacts),
    "read_memory_md": (ReadMemoryMdInput, read_memory_md),
    "write_memory_md": (WriteMemoryMdInput, write_memory_md),
}


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Lambda entrypoint. Dispatches on ``input.op`` to a typed handler."""
    payload = event.get("input") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return error("invalid_event", "expected event.input to be a JSON object")
    op = payload.get("op")
    if op not in DISPATCH:
        return error("unknown_op", f"op must be one of {sorted(DISPATCH)}, got {op!r}")
    model_cls, fn = DISPATCH[op]
    try:
        req = model_cls.model_validate(payload)
    except ValidationError as exc:
        return error("validation_error", json.loads(exc.json()))
    result = fn(req)
    logger.info("op handled", extra={"op": op})
    return {"ok": True, "op": op, "result": result}


def error(kind: str, detail: object) -> dict[str, Any]:
    """Log a rejection and return the standard error envelope."""
    logger.warning("op rejected", extra={"kind": kind, "detail": detail})
    return {"ok": False, "error": {"kind": kind, "detail": detail}}
