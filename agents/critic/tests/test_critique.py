"""Tests for critic.critique — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from critic.critique import Critique, Issue, render_critique, severity_counts


def make_critique() -> Critique:
    """Build a minimal valid critique covering all three severity buckets."""
    issues = [
        Issue(
            severity="high",
            path="docs/specs/add-healthz/design.md",
            symbol="components[2]",
            description="No concrete file path named for the projector Lambda.",
            recommendation="Add `lambdas/event_projector/src/event_projector/handler.py`.",
        ),
        Issue(
            severity="medium",
            path="docs/specs/add-healthz/requirements.md",
            symbol="AC-R-001-a",
            line=42,
            description="`supports OAuth` is not observable.",
            recommendation="Restate as `Given a Cognito JWT, when I POST /v1/runs, then 202`.",
        ),
        Issue(
            severity="low",
            path="docs/specs/add-healthz/tasks.md",
            symbol="T-003",
            description="Task estimated > 200 LOC.",
            recommendation="Split into T-003a (route) and T-003b (validation).",
        ),
    ]
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
            path="docs/x.md",
            description="x",
            recommendation="x",
        )


def test_issue_accepts_llm_natural_shape_with_optional_symbol_and_line() -> None:
    """The natural Strands ``structured_output`` shape: bare path + symbol."""
    issue = Issue.model_validate(
        {
            "severity": "medium",
            "path": "docs/specs/add-healthz/tasks.md",
            "symbol": "T-001",
            "description": "T-001 has no explicit acceptance criteria.",
            "recommendation": "Add a Given/When/Then.",
        },
    )
    assert issue.symbol == "T-001"
    assert issue.line is None


def test_severity_counts_complete() -> None:
    counts = severity_counts(make_critique())
    assert counts == {"high": 1, "medium": 1, "low": 1}


def test_empty_issues_rejected() -> None:
    """Strands surfaces this ValidationError to the agent for self-correction."""
    with pytest.raises(ValidationError):
        Critique(
            spec_slug="add-healthz",
            summary="No problems found.",
            issues=[],
            strengths=[],
        )


def test_render_critique_includes_counts_and_issues() -> None:
    out = render_critique(make_critique())
    assert "# Critique — `add-healthz`" in out
    assert "**1** high · **1** medium · **1** low" in out
    assert "### 1. [high] docs/specs/add-healthz/design.md (components[2])" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_critique_is_frozen() -> None:
    critique = make_critique()
    with pytest.raises(ValidationError):
        critique.spec_slug = "different"  # type: ignore[misc]  # frozen=True forbids assignment
