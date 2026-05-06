"""Tests for reviewer.review — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reviewer.review import Review, ReviewComment, render_review, severity_counts


def make_review(*, with_comments: bool = True, verdict: str = "request_changes") -> Review:
    """Build a minimal valid review; toggles comments and verdict."""
    comments = (
        [
            ReviewComment(
                severity="high",
                path="services/dashboard/src/dashboard/routes/health.py",
                symbol="healthz",
                line=14,
                description="Returns 200 even when the database connection is down.",
                suggestion="Add a `db.execute('SELECT 1')` probe and return 503 on failure.",
            ),
            ReviewComment(
                severity="medium",
                path="services/dashboard/tests/test_health.py",
                description="No test exercises the unauth path.",
                suggestion="Add a test that asserts /healthz returns 200 without a JWT.",
            ),
        ]
        if with_comments
        else []
    )
    return Review(
        task_id="T-001",
        verdict=verdict,  # ty: ignore[invalid-argument-type]
        summary="Implements the route, but the liveness check is too shallow.",
        comments=comments,
        strengths=["Clear FastAPI route.", "Type-annotated response model."],
    )


def test_minimal_review_validates() -> None:
    review = make_review()
    assert review.task_id == "T-001"
    assert len(review.comments) == 2


def test_invalid_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        Review(
            task_id="T-001",
            verdict="lgtm",  # ty: ignore[invalid-argument-type]
            summary="x",
        )


def test_invalid_severity_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewComment(
            severity="critical",  # ty: ignore[invalid-argument-type]
            path="x",
            description="x",
            suggestion="x",
        )


def test_review_comment_accepts_llm_natural_shape() -> None:
    """Strands ``structured_output`` shape: bare path + optional symbol/line."""
    comment = ReviewComment.model_validate(
        {
            "severity": "medium",
            "path": "services/dashboard/src/dashboard/routes/health.py",
            "symbol": "healthz",
            "description": "Missing graceful-shutdown handling.",
            "suggestion": "Set a SIGTERM handler that flips the response to 503.",
        },
    )
    assert comment.symbol == "healthz"
    assert comment.line is None


def test_severity_counts_complete() -> None:
    counts = severity_counts(make_review())
    assert counts == {"high": 1, "medium": 1, "low": 0}


def test_severity_counts_zero_for_empty_review() -> None:
    counts = severity_counts(make_review(with_comments=False, verdict="approve"))
    assert counts == {"high": 0, "medium": 0, "low": 0}


def test_render_review_includes_verdict_and_comments() -> None:
    out = render_review(make_review())
    assert "# Review — `T-001`" in out
    assert "Verdict: **request_changes**" in out
    assert "1 high · 1 medium · 0 low" in out
    assert "### 1. [high] `services/dashboard/src/dashboard/routes/health.py:14 (healthz)`" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_render_review_skips_comments_section_when_empty() -> None:
    out = render_review(make_review(with_comments=False, verdict="approve"))
    assert "## Comments" not in out
    assert "Verdict: **approve**" in out


def test_review_is_frozen() -> None:
    review = make_review()
    with pytest.raises(ValidationError):
        review.task_id = "T-002"  # type: ignore[misc]  # frozen=True forbids assignment
