"""End-to-end wiring tests for the gateway-mediated Tester.

Stubs the Strands ``MCPClient``, the agent loop, and the post-agent
gateway tool calls; verifies the daemon body holds the gateway session
open, builds the agent with it, uploads the report via
``call_gateway_tool(... put_artifact ...)``, posts the PR comment via
``call_gateway_tool(... comment_pr ...)``, and emits
``TEST_REPORT.READY``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import TesterInput
from tester import app
from tester.report import Report, ReportSummary
from tester.tools import report_s3_key


def make_input() -> TesterInput:
    return TesterInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        pr_url="https://github.com/owner/repo/pull/42",
        run_id="r-1",
        correlation_id="c-1",
    )


def make_report() -> Report:
    return Report(
        run_id="r-1",
        summary=ReportSummary(
            context="Diff adds /healthz route.",
            coverage_gap="No test for 503 path.",
            risk="Silent regression on degraded backend.",
        ),
        gaps=[],
        suggestions=[],
        strengths=["Happy-path test exists."],
    )


def test_run_tester_uses_gateway_and_publishes(
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
    monkeypatch.setattr(app, "analyze_gaps", MagicMock(return_value=make_report()))
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

    app.run_tester(make_input(), async_task_id=7)

    gateway_factory.assert_called_once_with()
    build.assert_called_once()
    assert build.call_args.kwargs["mcp_client"] is fake_client

    # Two gateway calls: put_artifact (upload report) + comment_pr (post comment).
    assert call_tool.call_count == 2
    upload_kwargs = call_tool.call_args_list[0].kwargs
    assert upload_kwargs["name"] == "artifact-tool___artifact_tool"
    assert upload_kwargs["arguments"]["op"] == "put_artifact"
    assert upload_kwargs["arguments"]["key"] == report_s3_key(run_id="r-1", revision_number=0)
    assert upload_kwargs["arguments"]["content"]

    comment_kwargs = call_tool.call_args_list[1].kwargs
    assert comment_kwargs["name"] == "repo-helper___repo_helper"
    assert comment_kwargs["arguments"]["op"] == "comment_pr"
    assert comment_kwargs["arguments"]["repo"] == "owner/repo"
    assert comment_kwargs["arguments"]["pr_number"] == 42
    assert comment_kwargs["arguments"]["body"]

    assert len(published) == 1
    assert published[0].type == "TEST_REPORT.READY"
    complete.assert_called_once_with(7)


def test_post_pr_comment_skips_on_unparseable_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unparseable pr_url logs a warning and returns; no gateway call."""
    call_tool = MagicMock()
    monkeypatch.setattr(app, "call_gateway_tool", call_tool)
    payload = TesterInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        pr_url="not-a-pr-url",
        run_id="r-1",
        correlation_id="c-1",
    )
    app.post_pr_comment(MagicMock(), payload=payload, report=make_report())
    call_tool.assert_not_called()


def test_post_pr_comment_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the gateway call raises, post_pr_comment logs and returns — never propagates."""
    monkeypatch.setattr(
        app,
        "call_gateway_tool",
        MagicMock(side_effect=RuntimeError("gateway 503")),
    )
    app.post_pr_comment(MagicMock(), payload=make_input(), report=make_report())
    # No exception — the test passes if we get here.


def test_run_tester_swallows_agent_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the agent loop raises, the run logs + completes the task without emitting READY.

    The tester is advisory — it does not emit RUN.FAILED on crash; the
    state machine relies on the reviewer's verdict to advance.
    """
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    monkeypatch.setattr(app, "gateway_mcp_client", MagicMock(return_value=fake_client))
    monkeypatch.setattr(app, "build_agent", MagicMock())
    monkeypatch.setattr(app, "analyze_gaps", MagicMock(side_effect=ValueError("agent blew up")))

    published: list[Any] = []
    monkeypatch.setattr(app, "publish", published.append)

    complete = MagicMock()
    monkeypatch.setattr(app.app, "complete_async_task", complete)

    app.run_tester(make_input(), async_task_id=8)

    assert fake_client.__exit__.called
    assert published == []
    complete.assert_called_once_with(8)
