"""Tests for ``common.triage`` — TriageDecision, MissingInformation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.triage import MissingInformation, TriageDecision


def test_proceed_validates() -> None:
    decision = TriageDecision(
        action="proceed",
        rationale="Issue has clear acceptance criteria and named target files.",
    )
    assert decision.action == "proceed"


def test_research_validates() -> None:
    decision = TriageDecision(
        action="research",
        rationale="Issue body links three RFCs to summarise.",
    )
    assert decision.action == "research"


def test_ask_with_questions_validates() -> None:
    decision = TriageDecision(
        action="ask",
        rationale="Acceptance criteria are missing.",
        missing_information=[
            MissingInformation(
                question="What status code should the endpoint return on auth failure?",
                why_needed="Implementer needs this to pick between 401 and 403.",
            ),
        ],
    )
    assert decision.action == "ask"
    assert len(decision.missing_information) == 1


def test_ask_without_questions_rejected() -> None:
    with pytest.raises(ValidationError):
        TriageDecision(action="ask", rationale="missing info but no items listed")


def test_decline_validates() -> None:
    decision = TriageDecision(
        action="decline",
        rationale="Duplicate of #42; marking as such.",
    )
    assert decision.action == "decline"
    assert decision.missing_information == []


def test_defer_validates() -> None:
    decision = TriageDecision(
        action="defer",
        rationale="Needs a product decision before implementation can start.",
    )
    assert decision.action == "defer"


def test_non_ask_must_not_list_missing_information() -> None:
    with pytest.raises(ValidationError):
        TriageDecision(
            action="proceed",
            rationale="x",
            missing_information=[MissingInformation(question="x", why_needed="x")],
        )


def test_confidence_bounded_zero_to_one() -> None:
    with pytest.raises(ValidationError):
        TriageDecision(
            action="decline",
            rationale="x",
            confidence=1.5,
        )


def test_unknown_action_rejected() -> None:
    with pytest.raises(ValidationError):
        TriageDecision.model_validate(
            {
                "action": "yolo",
                "rationale": "x",
            },
        )


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TriageDecision.model_validate(
            {
                "action": "decline",
                "rationale": "x",
                "extra_field": "nope",
            },
        )


def test_decision_is_frozen() -> None:
    decision = TriageDecision(action="decline", rationale="x")
    with pytest.raises(ValidationError):
        decision.action = "proceed"  # type: ignore[misc]  # frozen=True forbids assignment


def test_missing_information_question_required() -> None:
    with pytest.raises(ValidationError):
        MissingInformation(question="", why_needed="x")
