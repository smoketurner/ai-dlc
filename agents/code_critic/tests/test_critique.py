"""Tests for code_critic.critique — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_critic.critique import (
    Critique,
    Issue,
    render_critique,
    severity_counts,
)


def make_critique(*, with_issues: bool = True) -> Critique:
    """Build a minimal valid critique; toggles issue set for empty-critique tests."""
    issues = (
        [
            Issue(
                severity="high",
                path="services/dashboard/src/dashboard/routes/health.py",
                symbol="healthz",
                line=14,
                description="Returns 200 even when the database connection is down.",
                recommendation="Add a `db.execute('SELECT 1')` probe; return 503 on failure.",
                language="python",
                code_excerpt=(
                    "@router.get('/healthz')\n"
                    "def healthz() -> dict[str, bool]:\n"
                    "    return {'ok': True}  # <-- never checks db"
                ),
                references=["see services/dashboard/routes/auth.py — established db-probe pattern"],
            ),
            Issue(
                severity="medium",
                path="services/dashboard/src/dashboard/routes/health.py",
                description="No timeout on the db probe.",
                recommendation="Wrap the probe in `asyncio.wait_for(..., timeout=2.0)`.",
            ),
        ]
        if with_issues
        else [
            Issue(
                severity="low",
                path="README.md",
                description="Typo on line 12.",
                recommendation="`recieve` → `receive`.",
            ),
        ]
    )
    return Critique(
        run_id="01999999-9999-7999-9999-999999999999",
        summary="Adds healthz route; missing db check + timeout.",
        issues=issues,
        strengths=["Clear FastAPI route.", "Type-annotated response model."],
    )


def test_minimal_critique_validates() -> None:
    critique = make_critique()
    assert critique.run_id == "01999999-9999-7999-9999-999999999999"
    assert len(critique.issues) == 2


def test_critique_requires_at_least_one_issue() -> None:
    with pytest.raises(ValidationError):
        Critique(
            run_id="r-1",
            summary="x",
            issues=[],
        )


def test_severity_counts() -> None:
    counts = severity_counts(make_critique())
    assert counts == {"high": 1, "medium": 1, "low": 0}


def test_render_critique_includes_summary_and_issues() -> None:
    out = render_critique(make_critique())
    assert "# Code critique — run `01999999-9999-7999-9999-999999999999`" in out
    assert "**1** high · **1** medium · **0** low" in out
    assert "## Summary" in out
    assert "## Issues" in out
    assert "### 1. [high] `services/dashboard/src/dashboard/routes/health.py:14 (healthz)`" in out
    assert "```python" in out
    assert "# <-- never checks db" in out
    assert "**References:**" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_render_critique_omits_code_blocks_when_not_provided() -> None:
    """Issues without language/code_excerpt render as prose only."""
    out = render_critique(make_critique(with_issues=False))
    assert "```" not in out
    assert "**References:**" not in out


def test_critique_is_frozen() -> None:
    critique = make_critique()
    with pytest.raises(ValidationError):
        critique.run_id = "r-2"  # type: ignore[misc]  # frozen=True forbids assignment
