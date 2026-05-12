"""Tests for tester.report — pydantic validation + Markdown rendering."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tester.report import (
    Gap,
    Report,
    ReportSummary,
    Suggestion,
    gap_count,
    render_report,
    suggestion_count,
)


def make_report(*, with_findings: bool = True) -> Report:
    """Build a minimal valid report; toggles findings for empty-report tests."""
    gaps = (
        [
            Gap(
                path="docs/specs/add-healthz/requirements.md",
                symbol="AC-R-001-a",
                description="No test asserts /healthz returns 200 without auth.",
            ),
            Gap(
                path="services/dashboard/src/dashboard/routes/health.py",
                symbol="healthz",
                line=14,
                description="No test exercises the database-down path.",
            ),
        ]
        if with_findings
        else []
    )
    suggestions = (
        [
            Suggestion(
                name="test_healthz_returns_200_without_auth",
                test_kind="integration",
                given="the dashboard is running and Cognito is bypassed",
                when="I GET /healthz with no Authorization header",
                then="the response is 200 with body {ok: true}",
                covers=["AC-R-001-a"],
                language="python",
                proposed_test_code=(
                    "def test_healthz_returns_200_without_auth(client) -> None:\n"
                    "    resp = client.get('/healthz')\n"
                    "    assert resp.status_code == 200\n"
                    "    assert resp.json() == {'ok': True}"
                ),
                references=["see services/dashboard/tests/test_health.py — auth-bypass pattern"],
            ),
            Suggestion(
                name="test_healthz_returns_503_when_db_down",
                test_kind="unit",
                given="the database probe raises ConnectionError",
                when="I call healthz()",
                then="the response is 503 with body {ok: false, reason: 'db_unreachable'}",
                covers=["healthz"],
            ),
        ]
        if with_findings
        else []
    )
    return Report(
        task_id="T-001",
        summary=ReportSummary(
            context="Adds a /healthz liveness route on the dashboard service.",
            coverage_gap="No test exercises the unauthenticated or database-down paths.",
            risk=(
                "A degraded service would be marked healthy by upstream probes, "
                "delaying detection of outages."
            ),
        ),
        gaps=gaps,
        suggestions=suggestions,
        strengths=["Existing test asserts response schema."],
    )


def test_minimal_report_validates() -> None:
    report = make_report()
    assert report.task_id == "T-001"
    assert gap_count(report) == 2
    assert suggestion_count(report) == 2


def test_gap_accepts_llm_natural_shape() -> None:
    """Strands ``structured_output`` shape: bare path + optional symbol/line."""
    gap = Gap.model_validate(
        {
            "path": "services/dashboard/src/dashboard/routes/health.py",
            "symbol": "healthz",
            "description": "No test for the SIGTERM-shutdown path.",
        },
    )
    assert gap.symbol == "healthz"
    assert gap.line is None


def test_invalid_test_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        Suggestion(
            name="x",
            test_kind="snapshot",  # ty: ignore[invalid-argument-type]
            given="x",
            when="x",
            then="x",
            covers=["x"],
        )


def test_suggestion_requires_at_least_one_cover() -> None:
    with pytest.raises(ValidationError):
        Suggestion(
            name="x",
            test_kind="unit",
            given="x",
            when="x",
            then="x",
            covers=[],
        )


def test_render_report_includes_gaps_and_suggestions() -> None:
    out = render_report(make_report())
    assert "# Test report — `T-001`" in out
    assert "**2** gap(s) · **2** suggestion(s)" in out
    assert "- **Context:** Adds a /healthz liveness route" in out
    assert "- **Coverage gap:** No test exercises the unauthenticated" in out
    assert "- **Risk:** A degraded service would be marked healthy" in out
    assert "docs/specs/add-healthz/requirements.md (AC-R-001-a)" in out
    assert "### 1. `test_healthz_returns_200_without_auth` (integration)" in out
    assert "**Covers:** `AC-R-001-a`" in out
    assert "```python" in out
    assert "def test_healthz_returns_200_without_auth(client)" in out
    assert "**References:**" in out
    assert "- see services/dashboard/tests/test_health.py — auth-bypass pattern" in out
    assert "## Strengths" in out
    assert out.endswith("\n")


def test_render_report_skips_sections_when_empty() -> None:
    out = render_report(make_report(with_findings=False))
    assert "## Gaps" not in out
    assert "## Suggested tests" not in out
    assert "## Strengths" in out
    assert "```" not in out


def test_report_is_frozen() -> None:
    report = make_report()
    with pytest.raises(ValidationError):
        report.task_id = "T-002"  # type: ignore[misc]  # frozen=True forbids assignment
