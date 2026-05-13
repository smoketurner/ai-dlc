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
            path="runs/r-1/plan.md",
            symbol="Files to modify / create",
            description="No concrete file path named for the projector Lambda.",
            recommendation="Add `lambdas/event_projector/src/event_projector/handler.py`.",
        ),
        Issue(
            severity="medium",
            path="runs/r-1/plan.md",
            symbol="Approach",
            line=42,
            description="`supports OAuth` is not observable.",
            recommendation="Restate as `Given a Cognito JWT, when I POST /v1/runs, then 202`.",
        ),
        Issue(
            severity="low",
            path="runs/r-1/plan.md",
            symbol="Implementation steps",
            description="Step 3 is too large; estimate > 200 LOC.",
            recommendation="Split into a route step and a validation step.",
        ),
    ]
    return Critique(
        run_id="r-1",
        summary="Plan is mostly buildable; one high-severity gap on the projector path.",
        issues=issues,
        strengths=["Assumptions are explicit.", "Verification names concrete commands."],
    )


def test_minimal_critique_validates() -> None:
    critique = make_critique()
    assert critique.run_id == "r-1"
    assert len(critique.issues) == 3


def test_invalid_severity_rejected() -> None:
    with pytest.raises(ValidationError):
        Issue(
            severity="critical",  # ty: ignore[invalid-argument-type]
            path="runs/r-1/plan.md",
            description="x",
            recommendation="x",
        )


def test_issue_accepts_llm_natural_shape_with_optional_symbol_and_line() -> None:
    """The natural Strands ``structured_output`` shape: bare path + symbol."""
    issue = Issue.model_validate(
        {
            "severity": "medium",
            "path": "runs/r-1/plan.md",
            "symbol": "Implementation steps",
            "description": "Step 1 has no explicit verification.",
            "recommendation": "Add a `done when` clause.",
        },
    )
    assert issue.symbol == "Implementation steps"
    assert issue.line is None


def test_severity_counts_complete() -> None:
    counts = severity_counts(make_critique())
    assert counts == {"high": 1, "medium": 1, "low": 1}


def test_empty_issues_rejected() -> None:
    """Strands surfaces this ValidationError to the agent for self-correction."""
    with pytest.raises(ValidationError):
        Critique(
            run_id="r-1",
            summary="No problems found.",
            issues=[],
            strengths=[],
        )


def test_render_critique_includes_counts_and_issues() -> None:
    out = render_critique(make_critique())
    assert "# Critique — run `r-1`" in out
    assert "**1** high · **1** medium · **1** low" in out
    assert "### 1. [high] runs/r-1/plan.md (Files to modify / create)" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_critique_is_frozen() -> None:
    critique = make_critique()
    with pytest.raises(ValidationError):
        critique.run_id = "different"  # type: ignore[misc]  # frozen=True forbids assignment
