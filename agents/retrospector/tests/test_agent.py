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
def test_compose_message_for_task_approved_includes_pr_and_task() -> None:
    msg = compose_message(
        event_type="TASK.APPROVED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url="https://github.com/smoketurner/ai-dlc/pull/42",
        issue_url=None,
        spec_slug="lint-gate",
        task_id="T-001",
        reviewer="alice",
        reason=None,
    )
    assert "Event: TASK.APPROVED" in msg
    assert "Spec: lint-gate" in msg
    assert "Task: T-001" in msg
    assert "PR: https://github.com/smoketurner/ai-dlc/pull/42" in msg
    assert "Reviewer / sender: alice" in msg
    assert "list_pr_comments" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_for_run_cancel_uses_issue_url() -> None:
    msg = compose_message(
        event_type="RUN.CANCEL_REQUESTED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url=None,
        issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        spec_slug=None,
        task_id=None,
        reviewer="alice",
        reason="issue closed by alice",
    )
    assert "Event: RUN.CANCEL_REQUESTED" in msg
    assert "Issue: https://github.com/smoketurner/ai-dlc/issues/9" in msg
    assert "PR:" not in msg
    assert "issue closed by alice" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_omits_optional_fields_when_unset() -> None:
    msg = compose_message(
        event_type="SPEC.APPROVED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url="https://github.com/smoketurner/ai-dlc/pull/50",
        issue_url=None,
        spec_slug="lint-gate",
        task_id=None,
        reviewer=None,
        reason=None,
    )
    assert "Task:" not in msg
    assert "Reviewer / sender:" not in msg
