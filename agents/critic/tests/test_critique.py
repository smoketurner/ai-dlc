"""Tests for critic.critique — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from critic.critique import Critique, Issue, render_critique, severity_counts


def make_critique(*, with_issues: bool = True) -> Critique:
    """Build a minimal valid critique; toggles issues for empty-critique tests."""
    issues = (
        [
            Issue(
                severity="high",
                location="design.components[2]",
                description="No concrete file path named for the projector Lambda.",
                recommendation="Add `lambdas/event_projector/src/event_projector/handler.py`.",
            ),
            Issue(
                severity="medium",
                location="AC-R-001-a",
                description="`supports OAuth` is not observable.",
                recommendation="Restate as `Given a Cognito JWT, when I POST /v1/runs, then 202`.",
            ),
            Issue(
                severity="low",
                location="tasks[3]",
                description="Task estimated > 200 LOC.",
                recommendation="Split into T-003a (route) and T-003b (validation).",
            ),
        ]
        if with_issues
        else []
    )
    return Critique(
        spec_slug="add-healthz",
        summary="Spec is mostly buildable; one high-severity gap on the projector path.",
        issues=issues,
        strengths=["Acceptance criteria are testable.", "Tasks are ordered."],
    )


def test_minimal_critique_validates() -> None:
    critique = make_critique()
    assert critique.spec_slug == "add-healthz"
    assert len(critique.issues) == 3


def test_invalid_severity_rejected() -> None:
    with pytest.raises(ValidationError):
        Issue(
            severity="critical",  # ty: ignore[invalid-argument-type]
            location="x",
            description="x",
            recommendation="x",
        )


def test_severity_counts_complete() -> None:
    counts = severity_counts(make_critique())
    assert counts == {"high": 1, "medium": 1, "low": 1}


def test_severity_counts_zero_for_clean_critique() -> None:
    counts = severity_counts(make_critique(with_issues=False))
    assert counts == {"high": 0, "medium": 0, "low": 0}


def test_render_critique_includes_counts_and_issues() -> None:
    out = render_critique(make_critique())
    assert "# Critique — `add-healthz`" in out
    assert "**1** high · **1** medium · **1** low" in out
    assert "### 1. [high] design.components[2]" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_render_critique_skips_issues_section_when_empty() -> None:
    out = render_critique(make_critique(with_issues=False))
    assert "## Issues" not in out
    assert "## Strengths" in out


def test_critique_is_frozen() -> None:
    critique = make_critique()
    with pytest.raises(ValidationError):
        critique.spec_slug = "different"  # type: ignore[misc]  # frozen=True forbids assignment
