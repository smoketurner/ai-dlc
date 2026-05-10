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
        "add /healthz endpoint",
        project_slug="ai-dlc",
        prior_feedback=None,
        triggering_comment_body=None,
    )
    assert "Project: ai-dlc" in msg
    assert "Intent:" in msg
    assert "add /healthz endpoint" in msg
    assert "Reviewer feedback" not in msg
    assert "Additional user guidance" not in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_includes_triggering_comment_when_set() -> None:
    """``@aidlc-bot please also do X`` reaches the architect's prompt as guidance."""
    msg = compose_message(
        "add /healthz endpoint",
        project_slug="ai-dlc",
        prior_feedback=None,
        triggering_comment_body="please also include /readyz",
    )
    assert "Additional user guidance" in msg
    assert "please also include /readyz" in msg
    assert "Reviewer feedback" not in msg


@pytest.mark.usefixtures("memory_preamble_stub")
def test_compose_message_combines_triggering_comment_and_prior_feedback() -> None:
    """When both retry-feedback and a fresh comment are present, both surface."""
    msg = compose_message(
        "add /healthz endpoint",
        project_slug="ai-dlc",
        prior_feedback="missing 503-on-dependency-failure case",
        triggering_comment_body="please also include /readyz",
    )
    assert "Additional user guidance" in msg
    assert "please also include /readyz" in msg
    assert "Spec feedback to address" in msg
    assert "missing 503-on-dependency-failure case" in msg
