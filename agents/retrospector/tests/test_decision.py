"""Tests for the RetrospectiveDecision schema invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from retrospector.decision import RetrospectiveDecision


def test_no_lesson_decision_is_valid_with_only_rationale() -> None:
    """has_lesson=False allows empty section/summary/addition."""
    d = RetrospectiveDecision(
        has_lesson=False,
        rationale="Clean merge with no comments — nothing to learn.",
    )
    assert d.has_lesson is False
    assert d.section is None
    assert d.lesson_summary == ""
    assert d.memory_md_addition == ""


def test_lesson_decision_requires_section_summary_and_addition() -> None:
    d = RetrospectiveDecision(
        has_lesson=True,
        section="conventions",
        lesson_summary="Use FastAPI + Jinja2 + Alpine.js, not React.",
        memory_md_addition=(
            "- **Frontend stack**: FastAPI + Jinja2 + Alpine.js (CDN). "
            "_Why:_ reviewer rejected SPA approach in PR #42."
        ),
        rationale='Reviewer @jplock said: "we use FastAPI + Jinja2, not React".',
        confidence=0.9,
    )
    assert d.has_lesson is True
    assert d.section == "conventions"


def test_lesson_decision_without_section_is_rejected() -> None:
    with pytest.raises(ValidationError, match="has_lesson=True requires a section"):
        RetrospectiveDecision(
            has_lesson=True,
            lesson_summary="…",
            memory_md_addition="…",
            rationale="…",
        )


def test_lesson_decision_without_summary_is_rejected() -> None:
    with pytest.raises(ValidationError, match="lesson_summary"):
        RetrospectiveDecision(
            has_lesson=True,
            section="conventions",
            memory_md_addition="…",
            rationale="…",
        )


def test_no_lesson_with_section_set_is_rejected() -> None:
    with pytest.raises(ValidationError, match="section is set"):
        RetrospectiveDecision(
            has_lesson=False,
            section="conventions",
            rationale="…",
        )


def test_no_lesson_with_summary_set_is_rejected() -> None:
    with pytest.raises(ValidationError, match="lesson_summary is non-empty"):
        RetrospectiveDecision(
            has_lesson=False,
            lesson_summary="something",
            rationale="…",
        )
