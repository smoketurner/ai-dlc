"""Unit tests for the repo_helper Lambda handler (Phase 3 stub responses)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import repo_helper.handler as h
from aws_lambda_powertools.utilities.typing import LambdaContext


def ctx() -> LambdaContext:
    """Minimal stand-in for LambdaContext — covers the fields powertools reads."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="repo_helper-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


def test_open_pr_stub_response() -> None:
    out = h.handler(
        {
            "input": {
                "op": "open_pr",
                "repo": "smoketurner/ai-dlc",
                "base": "main",
                "head": "feature/foo",
                "title": "Add foo",
                "body": "Body",
            },
        },
        ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "open_pr"
    assert out["result"]["stub"] is True


def test_create_branch_validates_repo_format() -> None:
    out = h.handler(
        {"input": {"op": "create_branch", "repo": "no-slash", "branch": "x", "base": "main"}},
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_commit_files_requires_at_least_one_file() -> None:
    out = h.handler(
        {
            "input": {
                "op": "commit_files",
                "repo": "smoketurner/ai-dlc",
                "branch": "main",
                "message": "msg",
                "files": [],
            },
        },
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_unknown_op() -> None:
    out = h.handler({"input": {"op": "delete_repo"}}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_op"


def test_invalid_event() -> None:
    out = h.handler({}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "invalid_event"
