"""Tests for retrospector.agent — message composition wiring."""

from __future__ import annotations

import pytest

from retrospector.agent import compose_capture_message, compose_consolidate_message


@pytest.fixture
def memory_preamble_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the memory preamble — we're testing prompt composition, not retrieval."""
    monkeypatch.setattr(
        "retrospector.agent.agent_memory_preamble",
        lambda *, project_slug, query: f"<preamble for {project_slug}: {query[:32]}>",
    )


@pytest.mark.usefixtures("memory_preamble_stub")
def test_capture_message_for_run_completed_includes_pr() -> None:
    msg = compose_capture_message(
        event_type="RUN.COMPLETED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url="https://github.com/smoketurner/ai-dlc/pull/42",
        issue_url=None,
        reason=None,
        verdict=None,
        pr_comment_body=None,
    )
    assert "Event: RUN.COMPLETED" in msg
    assert "Impl PR: https://github.com/smoketurner/ai-dlc/pull/42" in msg
    assert "list_pr_comments" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_capture_message_for_review_ready_includes_verdict() -> None:
    msg = compose_capture_message(
        event_type="REVIEW.READY",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url="https://github.com/smoketurner/ai-dlc/pull/42",
        issue_url=None,
        reason=None,
        verdict="request_changes",
        pr_comment_body=None,
    )
    assert "Reviewer verdict: request_changes" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_capture_message_for_human_mention_quotes_comment_body() -> None:
    msg = compose_capture_message(
        event_type="IMPL.ITERATION_REQUESTED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url="https://github.com/smoketurner/ai-dlc/pull/42",
        issue_url=None,
        reason=None,
        verdict=None,
        pr_comment_body="@aidlc-bot the pagination helper exists; use it.",
    )
    assert "pagination helper exists" in msg
    assert "highest-signal" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_capture_message_for_run_cancel_uses_issue_url() -> None:
    msg = compose_capture_message(
        event_type="RUN.CANCEL_REQUESTED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url=None,
        issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        reason="issue closed by alice",
        verdict=None,
        pr_comment_body=None,
    )
    assert "Event: RUN.CANCEL_REQUESTED" in msg
    assert "Source issue: https://github.com/smoketurner/ai-dlc/issues/9" in msg
    assert "Impl PR:" not in msg
    assert "issue closed by alice" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_capture_message_omits_optional_fields_when_unset() -> None:
    msg = compose_capture_message(
        event_type="RUN.FAILED",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        pr_url=None,
        issue_url=None,
        reason=None,
        verdict=None,
        pr_comment_body=None,
    )
    assert "Event: RUN.FAILED" in msg
    assert "Impl PR:" not in msg
    assert "Source issue:" not in msg
    assert "Reason / context" not in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_consolidate_message_for_target_repo_lists_project_and_buffer() -> None:
    msg = compose_consolidate_message(
        destination="target_repo",
        project_slug="ai-dlc",
        target_repo="smoketurner/ai-dlc",
        buffer_content="## event_id=evt-1 — 2026-05-15T12:00:00\n\n```json\n{...}\n```",
    )
    assert "Destination: target_repo" in msg
    assert "Project: ai-dlc" in msg
    assert "event_id=evt-1" in msg
    assert "shipped_event_ids" in msg
    assert "discarded_event_ids" in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_consolidate_message_for_platform_skips_project_line() -> None:
    msg = compose_consolidate_message(
        destination="platform",
        project_slug="aidlc-platform",
        target_repo="smoketurner/ai-dlc",
        buffer_content="(empty)",
    )
    assert "Destination: platform" in msg
    assert "Platform repo: smoketurner/ai-dlc" in msg
    assert "Project: " not in msg
