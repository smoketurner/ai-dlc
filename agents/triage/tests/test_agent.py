"""Tests for triage.agent — message composition + structured-output wiring."""

from __future__ import annotations

from typing import Any

import pytest

from common.runtime import TriageInput
from common.triage import TriageDecision
from triage import agent as triage_agent
from triage.agent import compose_message, triage_issue


def make_input(**overrides: Any) -> TriageInput:
    """Build a minimal valid TriageInput. Override fields via kwargs."""
    base: dict[str, Any] = {
        "project_slug": "ai-dlc",
        "target_repo": "owner/name",
        "issue_url": "https://github.com/owner/name/issues/42",
        "issue_number": 42,
        "issue_title": "Add /healthz",
        "issue_body": "I want a healthz endpoint that returns 200.",
        "issue_type": "Feature",
        "issue_labels": ["priority:medium"],
        "prior_triage_count": 0,
        "prior_human_comments": [],
        "run_id": "01956000-0000-7000-0000-000000000001",
        "correlation_id": "01956000-0000-7000-0000-000000000002",
    }
    base.update(overrides)
    return TriageInput(**base)


def test_compose_message_includes_issue_metadata() -> None:
    payload = make_input()
    msg = compose_message(payload)
    assert "Issue: https://github.com/owner/name/issues/42" in msg
    assert "Title: Add /healthz" in msg
    assert "Type: Feature" in msg
    assert "Labels: priority:medium" in msg
    assert "I want a healthz endpoint that returns 200." in msg
    assert "Prior triage rounds" not in msg


def test_compose_message_handles_unspecified_type_and_no_labels() -> None:
    payload = make_input(issue_type=None, issue_labels=[])
    msg = compose_message(payload)
    assert "Type: unspecified" in msg
    assert "Labels: (none)" in msg


def test_compose_message_handles_empty_body() -> None:
    payload = make_input(issue_body="")
    msg = compose_message(payload)
    assert "Body:\n(empty)" in msg


def test_compose_message_includes_prior_rounds_when_present() -> None:
    payload = make_input(
        prior_triage_count=1,
        prior_human_comments=[
            "I want a 200 response with body {ok: true}",
            "Returning {ok: true} on success is fine; 503 on dependency failure.",
        ],
    )
    msg = compose_message(payload)
    assert "Prior triage rounds: 1" in msg
    assert "Human replies since the last triage round:" in msg
    assert "[1] I want a 200 response" in msg
    assert "[2] Returning {ok: true}" in msg


def test_compose_message_includes_triggering_comment_when_set() -> None:
    """``@aidlc-bot please reconsider X`` reaches the triage prompt."""
    payload = make_input(triggering_comment_body="please reconsider — we want a 503 on backend down")
    msg = compose_message(payload)
    assert "User comment that retriggered this triage round:" in msg
    assert "please reconsider — we want a 503 on backend down" in msg


def test_compose_message_omits_triggering_comment_section_when_none() -> None:
    """The retriggered-comment block is hidden when no comment is attached."""
    payload = make_input(triggering_comment_body=None)
    msg = compose_message(payload)
    assert "retriggered this triage round" not in msg


def test_triage_issue_wires_structured_output_against_decision_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected = TriageDecision(
        action="proceed",
        workflow_kind="spec_driven",
        rationale="Issue has clear acceptance criteria.",
    )

    class FakeResult:
        def __init__(self, output: TriageDecision) -> None:
            self.structured_output = output

    class FakeAgent:
        def __call__(self, prompt: str, *, structured_output_model: type[Any]) -> FakeResult:
            captured["model"] = structured_output_model
            captured["message"] = prompt
            return FakeResult(expected)

    def fake_build_agent(run_id: str) -> FakeAgent:
        captured["run_id"] = run_id
        return FakeAgent()

    monkeypatch.setattr(triage_agent, "build_agent", fake_build_agent)
    payload = make_input()
    result = triage_issue(payload)
    assert result == expected
    assert captured["model"] is TriageDecision
    assert captured["run_id"] == payload.run_id
    assert "Issue: https://github.com/owner/name/issues/42" in captured["message"]
