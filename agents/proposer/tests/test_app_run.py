"""Envelope handling + repo_helper-invoke wiring for the gateway-mediated Proposer.

The end-to-end orchestration tests live in ``test_research_app.py``;
this module focuses on the helpers ``run_proposer`` adds on top of
``run_research`` — gateway session lifecycle and the
MCPToolResult-to-envelope decoder used by ``invoke_repo_helper``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import ProposerInput
from proposer import app


def make_input() -> ProposerInput:
    return ProposerInput(
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        trigger_reason="research",
        intent="https://example.com/post",
        issue_number=34,
        run_id="019e08a2-aaeb-75c1-b03e-a59ef84f1a1c",
        correlation_id="019e08a2-aaeb-75c1-b03e-a59ef84f1a20",
    )


def fake_repo_helper_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """Shape returned by MCPClient.call_tool_sync for a dict-returning tool."""
    return {
        "status": "success",
        "toolUseId": "tu-1",
        "content": [{"text": json.dumps(envelope)}],
        "structuredContent": envelope,
    }


def test_invoke_repo_helper_calls_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """invoke_repo_helper dispatches to call_gateway_tool with the op + fields merged."""
    envelope = {"ok": True, "op": "comment_issue", "result": {}}
    call_tool = MagicMock(return_value=fake_repo_helper_result(envelope))
    monkeypatch.setattr(app, "call_gateway_tool", call_tool)

    mcp_client = MagicMock()
    out = app.invoke_repo_helper(
        mcp_client,
        op="comment_issue",
        repo="owner/repo",
        issue_number=42,
        body="hello",
    )

    assert out == envelope
    kwargs = call_tool.call_args.kwargs
    assert kwargs["name"] == "repo-helper___repo_helper"
    assert kwargs["arguments"] == {
        "op": "comment_issue",
        "repo": "owner/repo",
        "issue_number": 42,
        "body": "hello",
    }


def test_invoke_repo_helper_raises_on_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``ok: false`` envelope surfaces as a RuntimeError so the run fails loudly."""
    err_envelope = {"ok": False, "error": {"kind": "not_found"}}
    monkeypatch.setattr(
        app,
        "call_gateway_tool",
        MagicMock(return_value=fake_repo_helper_result(err_envelope)),
    )
    with pytest.raises(RuntimeError, match=r"repo_helper\.comment_issue failed"):
        app.invoke_repo_helper(MagicMock(), op="comment_issue", repo="owner/repo")


def test_run_proposer_swallows_run_research_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When run_research raises, the daemon logs and acknowledges the task without escalating."""
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    monkeypatch.setattr(app, "gateway_mcp_client", MagicMock(return_value=fake_client))
    monkeypatch.setattr(
        app,
        "run_research",
        MagicMock(side_effect=RuntimeError("synthetic")),
    )

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_proposer(make_input(), async_task_id=9)

    assert fake_client.__exit__.called
    complete.assert_called_once_with(9)
