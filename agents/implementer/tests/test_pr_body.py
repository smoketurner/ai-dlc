"""Tests for ``implementer.client.render_pr_body``."""

from __future__ import annotations

import pytest

from common.runtime import ImplementerInput
from implementer.client import render_pr_body
from implementer.finish import FinishReport, TestResult


@pytest.fixture
def payload() -> ImplementerInput:
    return ImplementerInput(
        project_slug="ai-dlc",
        spec_slug="add-healthz",
        spec_s3_prefix="specs/add-healthz/",
        task_id="T-001",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
    )


def test_render_pr_body_minimal_done(payload: ImplementerInput) -> None:
    report = FinishReport(summary="Added /healthz endpoint.", status="done")
    body = render_pr_body(payload, task_title="Add /healthz endpoint", report=report)
    assert "## T-001: Add /healthz endpoint" in body
    assert "### Summary" in body
    assert "Added /healthz endpoint." in body
    assert "### Files changed" not in body
    assert "### Tests" not in body
    assert "### Risks" not in body
    assert payload.run_id in body
    assert payload.correlation_id in body
    assert "docs/specs/add-healthz/" in body
    # Webhook receiver's RUN_META_RE needs both `_run_id:_` and `gate_ref:`
    # to find the right pending task token.
    assert "gate_ref: task:T-001" in body


def test_render_pr_body_full(payload: ImplementerInput) -> None:
    report = FinishReport(
        summary="Added /healthz endpoint and unit tests.",
        files_changed=["app/main.py", "tests/test_health.py"],
        tests_run=[
            TestResult(name="test_health_returns_200", status="pass"),
            TestResult(name="test_health_under_load", status="skip"),
        ],
        risks=["depends on FastAPI startup ordering"],
        status="done",
    )
    body = render_pr_body(payload, task_title="Add /healthz endpoint", report=report)
    # GitHub already shows the diff; the agent's self-reported file list is
    # always omitted to avoid misleading reviewers about scope.
    assert "### Files changed" not in body
    assert "- `app/main.py`" not in body
    assert "### Tests" in body
    assert "- `test_health_returns_200` — pass" in body
    assert "- `test_health_under_load` — skip" in body
    assert "### Risks" in body
    assert "- depends on FastAPI startup ordering" in body


def test_render_pr_body_omits_empty_sections(payload: ImplementerInput) -> None:
    report = FinishReport(
        summary="Tweaked a constant.",
        files_changed=["app/config.py"],
        # no tests, no risks
        status="done",
    )
    body = render_pr_body(payload, task_title="Tweak default", report=report)
    assert "### Files changed" not in body
    assert "### Tests" not in body
    assert "### Risks" not in body


def test_render_pr_body_none_report_uses_fallback(payload: ImplementerInput) -> None:
    body = render_pr_body(payload, task_title="Add /healthz endpoint", report=None)
    assert "did not call" in body or "without calling" in body
    assert "finish" in body
    assert payload.run_id in body
    assert payload.correlation_id in body
    # Fallback body must also carry the webhook gate_ref so HITL still works
    # when the agent skipped finish.
    assert "gate_ref: task:T-001" in body


def test_render_pr_body_does_not_include_chain_of_thought(payload: ImplementerInput) -> None:
    """The body must not contain free-form text outside the Summary section."""
    report = FinishReport(
        summary="Added /healthz endpoint.",
        files_changed=["app/main.py"],
        status="done",
    )
    body = render_pr_body(payload, task_title="Add /healthz endpoint", report=report)
    # No old-format "Implementer notes" section.
    assert "Implementer notes" not in body
    # No leaking spec headers.
    assert "# Requirements" not in body
    assert "# Design" not in body


def test_render_pr_body_under_2kb_for_typical_report(payload: ImplementerInput) -> None:
    """The plan targets <2KB bodies. Verify a typical report stays well under."""
    report = FinishReport(
        summary="x" * 500,
        files_changed=[f"path/file_{i}.py" for i in range(10)],
        tests_run=[TestResult(name=f"test_{i}", status="pass") for i in range(10)],
        risks=["risk one", "risk two"],
        status="done",
    )
    body = render_pr_body(payload, task_title="Big task", report=report)
    assert len(body) < 2048
