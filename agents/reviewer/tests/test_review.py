"""Tests for reviewer.review — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from reviewer.review import (
    AssumptionCheck,
    Review,
    ReviewComment,
    ReviewSummary,
    render_review,
    severity_counts,
)


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
                language="python",
                code_excerpt=(
                    "@router.get('/healthz')\n"
                    "def healthz() -> dict[str, bool]:\n"
                    "    return {'ok': True}  # <-- bug: never checks db"
                ),
                suggested_code=(
                    "@router.get('/healthz')\n"
                    "def healthz(db: Session = Depends(get_db)) -> dict[str, bool]:\n"
                    "    db.execute('SELECT 1')\n"
                    "    return {'ok': True}"
                ),
                references=["see services/dashboard/routes/auth.py — established db-probe pattern"],
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
        run_id="01999999-9999-7999-9999-999999999999",
        verdict=verdict,  # ty: ignore[invalid-argument-type]
        summary=ReviewSummary(
            context="Adds a /healthz liveness route on the dashboard service.",
            issue="The probe returns 200 unconditionally — it doesn't check the database.",
            actual_vs_expected=(
                "A request with the database down returns 200 OK; expected 503 with reason."
            ),
            impact="Production health checks would mark a degraded service healthy.",
        ),
        comments=comments,
        strengths=["Clear FastAPI route.", "Type-annotated response model."],
    )


def test_minimal_review_validates() -> None:
    review = make_review()
    assert review.run_id == "01999999-9999-7999-9999-999999999999"
    assert len(review.comments) == 2


def test_invalid_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        Review(
            run_id="r-1",
            verdict="lgtm",  # ty: ignore[invalid-argument-type]
            summary=ReviewSummary(context="x", impact="x"),
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
    assert out.startswith("# Code review\n")
    assert "<!-- ai-dlc-run: 01999999-9999-7999-9999-999999999999 -->" in out
    assert "Verdict: **request_changes**" in out
    assert "1 high · 1 medium · 0 low" in out
    assert "- **Context:** Adds a /healthz liveness route" in out
    assert "- **Issue:** The probe returns 200 unconditionally" in out
    assert "- **Actual vs. expected:** A request with the database down" in out
    assert "- **Impact:** Production health checks would mark a degraded service healthy." in out
    assert "### 1. [high] `services/dashboard/src/dashboard/routes/health.py:14 (healthz)`" in out
    assert "```python" in out
    assert "# <-- bug: never checks db" in out
    assert "db.execute('SELECT 1')" in out
    assert "**References:**" in out
    assert "- see services/dashboard/routes/auth.py — established db-probe pattern" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_render_review_skips_comments_section_when_empty() -> None:
    out = render_review(make_review(with_comments=False, verdict="approve"))
    assert "## Comments" not in out
    assert "Verdict: **approve**" in out


def test_render_review_omits_optional_summary_fields() -> None:
    """An ``approve`` review may have no Issue / Actual-vs-expected bullets."""
    review = Review(
        run_id="r-1",
        verdict="approve",
        summary=ReviewSummary(
            context="Adds a /healthz liveness route.",
            impact="None; existing endpoints unchanged.",
        ),
    )
    out = render_review(review)
    assert "- **Context:** Adds a /healthz liveness route." in out
    assert "- **Impact:** None; existing endpoints unchanged." in out
    assert "- **Issue:**" not in out
    assert "- **Actual vs. expected:**" not in out


def test_render_review_omits_code_blocks_when_not_provided() -> None:
    """Comments without language/code_excerpt render as prose only."""
    review = Review(
        run_id="r-1",
        verdict="comment",
        summary=ReviewSummary(context="Minor cleanup.", impact="None."),
        comments=[
            ReviewComment(
                severity="low",
                path="README.md",
                description="Typo on line 12.",
                suggestion="`recieve` → `receive`.",
            ),
        ],
    )
    out = render_review(review)
    assert "```" not in out  # no fenced block
    assert "**References:**" not in out


def test_review_is_frozen() -> None:
    review = make_review()
    with pytest.raises(ValidationError):
        review.run_id = "r-2"  # type: ignore[misc]  # frozen=True forbids assignment


def test_assumption_check_round_trips() -> None:
    check = AssumptionCheck(
        assumption="The agent feeds output back to the same ClaudeSDKClient session.",
        verdict="rebutted",
        citation='Issue: "feed the failing tool\'s stderr/stdout back to the same session"',
    )
    assert check.verdict == "rebutted"
    assert "same session" in check.citation


def test_assumption_check_unsupported_allows_empty_citation() -> None:
    check = AssumptionCheck(
        assumption="The target repo has a Makefile at its root.",
        verdict="unsupported",
    )
    assert check.citation == ""


def test_assumption_check_rejects_unknown_verdict() -> None:
    with pytest.raises(ValidationError):
        AssumptionCheck(
            assumption="x",
            verdict="maybe",  # ty: ignore[invalid-argument-type]
        )


def test_render_review_includes_assumption_checks_section() -> None:
    review = make_review()
    review = review.model_copy(
        update={
            "assumption_checks": [
                AssumptionCheck(
                    assumption="Lint gate must run before push.",
                    verdict="confirmed",
                    citation='Issue: "Before pushing"',
                ),
            ],
        },
    )
    out = render_review(review)
    assert "## Architect assumption checks" in out
    assert "**[confirmed]** Lint gate must run before push." in out
    assert "Citation: Issue:" in out


def test_render_review_skips_assumption_checks_section_when_empty() -> None:
    out = render_review(make_review(with_comments=False, verdict="approve"))
    assert "## Architect assumption checks" not in out
