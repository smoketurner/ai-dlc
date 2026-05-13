"""End-to-end wiring tests for the gateway-mediated Architect.

Stubs the Strands ``MCPClient``, the agent loop, the gateway tool calls,
and the per-run repo grounding steps; verifies the daemon body holds
the gateway session open, builds the agent with it, reads the plan back
via ``call_gateway_tool(... get_artifact ...)``, and emits
``DESIGN.READY``. A separate failure-path test pins the
``RUN.FAILED`` emission when the agent loop raises.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from architect import app
from architect.tools import plan_s3_key
from common.runtime import ArchitectInput

PLAN_BODY = "## Context\nAdd /healthz route.\n"


def make_input() -> ArchitectInput:
    return ArchitectInput(
        project_slug="demo",
        intent="Add /healthz",
        run_id="r-1",
        correlation_id="c-1",
    )


def fake_get_artifact_result(content: str) -> dict[str, Any]:
    """Shape returned by MCPClient.call_tool_sync for a dict-returning tool."""
    envelope = {
        "ok": True,
        "op": "get_artifact",
        "result": {"key": plan_s3_key("r-1"), "content": content},
    }
    return {
        "status": "success",
        "toolUseId": "tu-1",
        "content": [{"text": json.dumps(envelope)}],
        "structuredContent": envelope,
    }


def stub_repo_grounding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass clone + sync steps; we're testing app wiring, not grounding."""
    monkeypatch.setattr(app, "clone_target_repo", MagicMock())
    monkeypatch.setattr(app, "sync_memory_md_from_clone", MagicMock())
    monkeypatch.setattr(app, "sync_stack_profile_from_clone", MagicMock())


def test_run_architect_uses_gateway_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon body opens the gateway, threads it into build_agent, fetches plan, emits READY."""
    stub_repo_grounding(monkeypatch)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    gateway_factory = MagicMock(return_value=fake_client)
    monkeypatch.setattr(app, "gateway_mcp_client", gateway_factory)

    fake_agent = MagicMock()
    fake_agent.event_loop_metrics = None
    build = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(app, "build_agent", build)
    monkeypatch.setattr(app, "generate_plan", MagicMock())
    monkeypatch.setattr(
        app,
        "usage_from_strands",
        MagicMock(return_value={"token_in": 1, "token_out": 2, "cost_usd": 0.0, "duration_ms": 0}),
    )

    call_tool = MagicMock(return_value=fake_get_artifact_result(PLAN_BODY))
    monkeypatch.setattr(app, "call_gateway_tool", call_tool)

    published: list[Any] = []
    monkeypatch.setattr(app, "publish", published.append)

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_architect(make_input(), task_id=7)

    gateway_factory.assert_called_once_with()
    build.assert_called_once()
    kwargs = build.call_args.kwargs
    assert kwargs["mcp_client"] is fake_client

    call_tool.assert_called_once()
    tool_kwargs = call_tool.call_args.kwargs
    assert tool_kwargs["name"] == "artifact_tool"
    assert tool_kwargs["arguments"] == {
        "op": "get_artifact",
        "key": plan_s3_key("r-1"),
    }

    assert len(published) == 1
    assert published[0].type == "DESIGN.READY"
    assert published[0].payload.plan_s3_key == plan_s3_key("r-1")
    complete.assert_called_once_with(7)


def test_run_architect_publishes_run_failed_on_agent_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the agent loop raises, RUN.FAILED is emitted and the task is completed."""
    stub_repo_grounding(monkeypatch)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    monkeypatch.setattr(app, "gateway_mcp_client", MagicMock(return_value=fake_client))
    monkeypatch.setattr(app, "build_agent", MagicMock())
    monkeypatch.setattr(
        app,
        "generate_plan",
        MagicMock(side_effect=ValueError("agent blew up")),
    )

    published: list[Any] = []
    monkeypatch.setattr(app, "publish", published.append)

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_architect(make_input(), task_id=8)

    assert fake_client.__exit__.called
    assert len(published) == 1
    assert published[0].type == "RUN.FAILED"
    assert published[0].payload.error_class == "ValueError"
    complete.assert_called_once_with(8)


def test_fetch_plan_body_returns_content_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_plan_body unwraps structuredContent.result.content."""
    monkeypatch.setattr(
        app,
        "call_gateway_tool",
        MagicMock(return_value=fake_get_artifact_result("hello")),
    )
    body = app.fetch_plan_body(MagicMock(), "r-1")
    assert body == "hello"


def test_fetch_plan_body_raises_when_ok_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error envelope surfaces as a RuntimeError so the run fails loudly."""
    err_envelope = {"ok": False, "error": {"kind": "not_found"}}
    monkeypatch.setattr(
        app,
        "call_gateway_tool",
        MagicMock(return_value={"structuredContent": err_envelope, "content": []}),
    )
    with pytest.raises(RuntimeError, match="error envelope"):
        app.fetch_plan_body(MagicMock(), "r-1")
