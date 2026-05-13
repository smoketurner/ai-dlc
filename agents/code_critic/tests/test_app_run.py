"""End-to-end wiring tests for the gateway-mediated Code-Critic.

Stubs the Strands ``MCPClient``, the agent loop, and the post-agent
gateway tool calls; verifies the daemon body holds the gateway session
open, builds the agent with it, uploads the critique via
``call_gateway_tool(... put_artifact ...)``, posts the PR comment via
``call_gateway_tool(... comment_pr ...)``, and emits
``CODE_CRITIQUE.READY``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from code_critic.critique import Critique, Issue
from code_critic.tools import critique_s3_key

from code_critic import app
from common.runtime import CodeCriticInput


def make_input() -> CodeCriticInput:
    return CodeCriticInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        pr_url="https://github.com/owner/repo/pull/42",
        run_id="r-1",
        correlation_id="c-1",
        source_issue_url="https://github.com/owner/repo/issues/7",
        source_issue_title="Add /healthz endpoint",
        source_issue_body="As an oncall, I want /healthz so I can probe liveness.",
    )


def make_critique() -> Critique:
    return Critique(
        run_id="r-1",
        summary="Diff implements the endpoint; one edge case is missing.",
        issues=[
            Issue(
                severity="medium",
                path="app.py",
                description="No 503 path when the backend is unhealthy.",
                recommendation="Add a try/except and return 503 on backend errors.",
            ),
        ],
    )


def test_run_code_critic_uses_gateway_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon opens gateway, threads it into build_agent, uploads + comments, emits READY."""
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    gateway_factory = MagicMock(return_value=fake_client)
    monkeypatch.setattr(app, "gateway_mcp_client", gateway_factory)

    fake_agent = MagicMock()
    fake_agent.event_loop_metrics = None
    build = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(app, "build_agent", build)
    monkeypatch.setattr(app, "critique_pr", MagicMock(return_value=make_critique()))
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

    app.run_code_critic(make_input(), async_task_id=7)

    gateway_factory.assert_called_once_with()
    build.assert_called_once()
    assert build.call_args.kwargs["mcp_client"] is fake_client

    # Two gateway calls: put_artifact (upload critique) + comment_pr (post comment).
    assert call_tool.call_count == 2
    upload_kwargs = call_tool.call_args_list[0].kwargs
    assert upload_kwargs["name"] == "artifact-tool___artifact_tool"
    assert upload_kwargs["arguments"]["op"] == "put_artifact"
    assert upload_kwargs["arguments"]["key"] == critique_s3_key(
        run_id="r-1",
        revision_number=0,
    )
    assert upload_kwargs["arguments"]["content"]

    comment_kwargs = call_tool.call_args_list[1].kwargs
    assert comment_kwargs["name"] == "repo-helper___repo_helper"
    assert comment_kwargs["arguments"]["op"] == "comment_pr"
    assert comment_kwargs["arguments"]["repo"] == "owner/repo"
    assert comment_kwargs["arguments"]["pr_number"] == 42
    assert comment_kwargs["arguments"]["body"]

    assert len(published) == 1
    assert published[0].type == "CODE_CRITIQUE.READY"
    complete.assert_called_once_with(7)


def test_post_pr_comment_skips_on_unparseable_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unparseable pr_url logs a warning and returns; no gateway call."""
    call_tool = MagicMock()
    monkeypatch.setattr(app, "call_gateway_tool", call_tool)
    payload = CodeCriticInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        pr_url="not-a-pr-url",
        run_id="r-1",
        correlation_id="c-1",
    )
    app.post_pr_comment(MagicMock(), payload=payload, critique=make_critique())
    call_tool.assert_not_called()


def test_post_pr_comment_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the gateway call raises, post_pr_comment logs and returns — never propagates."""
    monkeypatch.setattr(
        app,
        "call_gateway_tool",
        MagicMock(side_effect=RuntimeError("gateway 503")),
    )
    app.post_pr_comment(MagicMock(), payload=make_input(), critique=make_critique())
    # No exception — the test passes if we get here.


def test_run_code_critic_publishes_run_failed_on_exception(
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
        "critique_pr",
        MagicMock(side_effect=ValueError("agent blew up")),
    )

    published: list[Any] = []
    monkeypatch.setattr(app, "publish", published.append)

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_code_critic(make_input(), async_task_id=8)

    assert fake_client.__exit__.called
    assert len(published) == 1
    assert published[0].type == "RUN.FAILED"
    assert published[0].payload.error_class == "ValueError"
    complete.assert_called_once_with(8)
