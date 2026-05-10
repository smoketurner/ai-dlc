"""Tests for the RetrospectiveDecision schema invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from retrospector.decision import RetrospectiveDecision


def test_no_lesson_decision_is_valid_with_only_rationale() -> None:
    """has_lesson=False allows empty section/summary/addition/target_file."""
    d = RetrospectiveDecision(
        has_lesson=False,
        rationale="Clean merge with no comments — nothing to learn.",
    )
    assert d.has_lesson is False
    assert d.target_file is None
    assert d.section is None
    assert d.lesson_summary == ""
    assert d.memory_md_addition == ""


def test_memory_md_lesson_requires_section() -> None:
    d = RetrospectiveDecision(
        has_lesson=True,
        target_file="MEMORY.md",
        section="conventions",
        lesson_summary="Use FastAPI + Jinja2 + Alpine.js, not React.",
        memory_md_addition=(
            "- **Frontend stack**: FastAPI + Jinja2 + Alpine.js (CDN). "
            "_Why:_ reviewer rejected SPA approach in PR #42."
        ),
        rationale='Reviewer @jplock said: "we use FastAPI + Jinja2, not React".',
        confidence=0.9,
    )
    assert d.target_file == "MEMORY.md"
    assert d.section == "conventions"


def test_agents_md_lesson_forbids_section() -> None:
    d = RetrospectiveDecision(
        has_lesson=True,
        target_file="AGENTS.md",
        lesson_summary="This repo is research-only, never deployed.",
        memory_md_addition="## Research-only\n\nNot deployed to production.",
        rationale="Maintainer flagged in PR comment.",
        confidence=0.7,
    )
    assert d.target_file == "AGENTS.md"
    assert d.section is None


def test_lesson_without_target_file_is_rejected() -> None:
    with pytest.raises(ValidationError, match="target_file"):
        RetrospectiveDecision(
            has_lesson=True,
            section="conventions",
            lesson_summary="x",
            memory_md_addition="y",
            rationale="z",
        )


def test_memory_md_lesson_without_section_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"target_file=MEMORY\.md requires section"):
        RetrospectiveDecision(
            has_lesson=True,
            target_file="MEMORY.md",
            lesson_summary="x",
            memory_md_addition="y",
            rationale="z",
        )


def test_agents_md_lesson_with_section_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"target_file=AGENTS\.md must not set section"):
        RetrospectiveDecision(
            has_lesson=True,
            target_file="AGENTS.md",
            section="conventions",
            lesson_summary="x",
            memory_md_addition="y",
            rationale="z",
        )


def test_lesson_without_summary_is_rejected() -> None:
    with pytest.raises(ValidationError, match="lesson_summary"):
        RetrospectiveDecision(
            has_lesson=True,
            target_file="MEMORY.md",
            section="conventions",
            memory_md_addition="…",
            rationale="…",
        )


def test_no_lesson_with_target_file_set_is_rejected() -> None:
    with pytest.raises(ValidationError, match="target_file is set"):
        RetrospectiveDecision(
            has_lesson=False,
            target_file="MEMORY.md",
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
