"""End-to-end wiring tests for the gateway-mediated Critic.

Stubs the Strands ``MCPClient``, the agent loop, and the gateway tool
calls; verifies the handler spawns the run with a copied context and
the daemon uploads the critique via ``call_gateway_tool``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import CriticInput
from critic import app
from critic.critique import Critique, Issue
from critic.tools import critique_s3_key


def make_input() -> CriticInput:
    return CriticInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        intent="Add /healthz",
        run_id="r-1",
        correlation_id="c-1",
    )


def make_critique() -> Critique:
    return Critique(
        run_id="r-1",
        summary="ok",
        issues=[
            Issue(
                severity="low",
                path="runs/r-1/plan.md",
                description="nit only.",
                recommendation="ok as-is.",
            ),
        ],
    )


def test_handler_dispatches_via_copy_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler spawns a daemon thread whose target runs under a copied context."""
    monkeypatch.setattr(app.app, "add_async_task", MagicMock(return_value=42))

    captured: dict[str, Any] = {}

    class FakeThread:
        def __init__(self, *, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(app.threading, "Thread", FakeThread)

    out = app.handler(make_input().model_dump())

    assert out["status"] == "dispatched"
    assert out["task_id"] == 42
    assert captured["started"] is True
    assert captured["daemon"] is True
    # target is the bound `Context.run` method; args carry (run_critic, payload, task_id).
    assert captured["target"].__name__ == "run"
    assert captured["args"][0] is app.run_critic
    assert isinstance(captured["args"][1], CriticInput)
    assert captured["args"][2] == 42


def test_run_critic_uploads_via_mcp_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon body wires gateway tools into the agent, uploads via MCP, emits READY."""
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    gateway_factory = MagicMock(return_value=fake_client)
    monkeypatch.setattr(app, "gateway_mcp_client", gateway_factory)

    fake_agent = MagicMock()
    fake_agent.event_loop_metrics = None
    build = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(app, "build_agent", build)
    monkeypatch.setattr(app, "critique_plan", MagicMock(return_value=make_critique()))
    monkeypatch.setattr(
        app,
        "usage_from_strands",
        MagicMock(return_value={"token_in": 1, "token_out": 2, "cost_usd": 0.0, "duration_ms": 0}),
    )

    call_tool = MagicMock()
    monkeypatch.setattr(app, "call_gateway_tool", call_tool)

    published: list[Any] = []
    monkeypatch.setattr(app, "publish", published.append)

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_critic(make_input(), task_id=7)

    gateway_factory.assert_called_once_with()
    build.assert_called_once()
    kwargs = build.call_args.kwargs
    assert kwargs["mcp_client"] is fake_client

    call_tool.assert_called_once()
    tool_kwargs = call_tool.call_args.kwargs
    assert tool_kwargs["name"] == "artifact-tool___artifact_tool"
    assert tool_kwargs["arguments"]["op"] == "put_artifact"
    assert tool_kwargs["arguments"]["key"] == critique_s3_key("r-1")
    assert tool_kwargs["arguments"]["content"]  # rendered markdown body

    assert len(published) == 1
    assert published[0].type == "CRITIQUE.READY"
    complete.assert_called_once_with(7)


def test_run_critic_publishes_run_failed_on_agent_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the agent loop raises, RUN.FAILED is emitted and the task is completed."""
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    monkeypatch.setattr(app, "gateway_mcp_client", MagicMock(return_value=fake_client))
    monkeypatch.setattr(app, "build_agent", MagicMock())
    monkeypatch.setattr(
        app,
        "critique_plan",
        MagicMock(side_effect=ValueError("agent blew up")),
    )

    published: list[Any] = []
    monkeypatch.setattr(app, "publish", published.append)

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_critic(make_input(), task_id=8)

    assert fake_client.__exit__.called
    assert len(published) == 1
    assert published[0].type == "RUN.FAILED"
    assert published[0].payload.error_class == "ValueError"
    complete.assert_called_once_with(8)
