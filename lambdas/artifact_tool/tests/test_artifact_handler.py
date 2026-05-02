"""Unit tests for the artifact_tool Lambda handler."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from artifact_tool.handler import handler, s3
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws

ARTIFACTS = "test-artifacts"
MEMORY_MD = "test-memory-md"


def ctx() -> LambdaContext:
    """Minimal stand-in for LambdaContext — covers the fields powertools reads."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="artifact_tool-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars + spin up a moto-mocked S3 with the two buckets."""
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("AIDLC_MEMORY_MD_BUCKET", MEMORY_MD)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    s3.cache_clear()
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=ARTIFACTS)
        client.create_bucket(Bucket=MEMORY_MD)
        yield
    s3.cache_clear()


def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    return handler({"input": payload}, ctx())


def test_put_then_get_roundtrip() -> None:
    out = invoke({"op": "put_artifact", "key": "ADRs/0001.md", "content": "# ADR"})
    assert out == {
        "ok": True,
        "op": "put_artifact",
        "result": {"bucket": ARTIFACTS, "key": "ADRs/0001.md"},
    }
    out = invoke({"op": "get_artifact", "key": "ADRs/0001.md"})
    assert out["ok"] is True
    assert out["result"] == {"key": "ADRs/0001.md", "content": "# ADR"}


def test_list_under_prefix() -> None:
    for k in ("a/1.md", "a/2.md", "b/1.md"):
        invoke({"op": "put_artifact", "key": k, "content": "x"})
    out = invoke({"op": "list_artifacts", "prefix": "a/"})
    assert out["ok"] is True
    assert sorted(out["result"]["keys"]) == ["a/1.md", "a/2.md"]


def test_write_then_read_memory_md() -> None:
    out = invoke(
        {
            "op": "write_memory_md",
            "project_slug": "demo",
            "session_id": "s1",
            "content": "# Project Memory\n\n## Overview\n\nhello",
        },
    )
    assert out["ok"] is True
    assert out["result"]["canonical_key"] == "projects/demo/MEMORY.md"
    out = invoke({"op": "read_memory_md", "project_slug": "demo"})
    assert out["ok"] is True
    assert out["result"]["content"].startswith("# Project Memory")


def test_unknown_op_returns_error() -> None:
    out = invoke({"op": "lol"})
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_op"


def test_missing_input_returns_error() -> None:
    out = handler({}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "invalid_event"


def test_validation_error_on_missing_field() -> None:
    out = invoke({"op": "put_artifact", "key": "x"})  # missing content
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"
