"""Tests for retrospector.agent — message composition wiring."""

from __future__ import annotations

import pytest

from retrospector.agent import compose_message


@pytest.fixture
def memory_preamble_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the memory preamble — we're testing prompt composition, not retrieval."""
    monkeypatch.setattr(
        "retrospector.agent.agent_memory_preamble",
        lambda *, project_slug, query: f"<preamble for {project_slug}: {query[:32]}>",
    )


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_for_run_completed_includes_pr() -> None:
    msg = compose_message(
        event_type="RUN.COMPLETED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url="https://github.com/smoketurner/ai-dlc/pull/42",
        issue_url=None,
        reason=None,
    )
    assert "Event: RUN.COMPLETED" in msg
    assert "Impl PR: https://github.com/smoketurner/ai-dlc/pull/42" in msg
    assert "list_pr_comments" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_for_run_cancel_uses_issue_url() -> None:
    msg = compose_message(
        event_type="RUN.CANCEL_REQUESTED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url=None,
        issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        reason="issue closed by alice",
    )
    assert "Event: RUN.CANCEL_REQUESTED" in msg
    assert "Source issue: https://github.com/smoketurner/ai-dlc/issues/9" in msg
    assert "Impl PR:" not in msg
    assert "issue closed by alice" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_omits_optional_fields_when_unset() -> None:
    msg = compose_message(
        event_type="RUN.FAILED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url=None,
        issue_url=None,
        reason=None,
    )
    assert "Event: RUN.FAILED" in msg
    assert "Impl PR:" not in msg
    assert "Source issue:" not in msg
    assert "Reason / context" not in msg
