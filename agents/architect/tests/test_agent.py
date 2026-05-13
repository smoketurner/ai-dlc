"""Tests for architect.agent — message composition wiring."""

from __future__ import annotations

import pytest

from architect.agent import compose_message


@pytest.fixture
def memory_preamble_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the memory preamble — we're testing prompt composition, not retrieval."""
    monkeypatch.setattr(
        "architect.agent.agent_memory_preamble",
        lambda *, project_slug, query: f"<preamble for {project_slug}: {query[:20]}>",
    )


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_threads_intent_only_by_default() -> None:
    msg = compose_message(
        intent="add /healthz endpoint",
        project_slug="ai-dlc",
        run_id="r-1",
        triggering_comment_body=None,
        source_issue_url=None,
        source_issue_title=None,
        source_issue_body=None,
    )
    assert "Project: ai-dlc" in msg
    assert "Run id: r-1" in msg
    assert "Intent:" in msg
    assert "add /healthz endpoint" in msg
    assert "Additional user guidance" not in msg
    assert "Issue body:" not in msg
    # Plan-writing instruction is always present.
    assert "put_artifact(key='runs/r-1/plan.md'" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_includes_triggering_comment_when_set() -> None:
    """``@aidlc-bot please also do X`` reaches the architect's prompt as guidance."""
    msg = compose_message(
        intent="add /healthz endpoint",
        project_slug="ai-dlc",
        run_id="r-1",
        triggering_comment_body="please also include /readyz",
        source_issue_url=None,
        source_issue_title=None,
        source_issue_body=None,
    )
    assert "Additional user guidance" in msg
    assert "please also include /readyz" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_includes_issue_context() -> None:
    """Issue title + body + URL surface in the prompt for grounding."""
    msg = compose_message(
        intent="Add /healthz endpoint",
        project_slug="ai-dlc",
        run_id="r-1",
        triggering_comment_body=None,
        source_issue_url="https://github.com/owner/repo/issues/42",
        source_issue_title="Add healthz",
        source_issue_body="As an oncall I want /healthz so I can probe liveness.",
    )
    assert "https://github.com/owner/repo/issues/42" in msg
    assert "Issue title: Add healthz" in msg
    assert "Issue body:" in msg
    assert "probe liveness" in msg
