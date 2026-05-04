"""Tests for drift_detector — bucket comparison + report shape."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import drift_detector.handler as h
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from drift_detector.handler import CaseResult, compute_drift, group_by_case, window_stats


def ctx() -> LambdaContext:
    """Minimal stand-in for LambdaContext."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="drift-detector-test",
            memory_limit_in_mb=512,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


def make(case: str, *, days_ago: float, passed: bool) -> CaseResult:
    """Build a synthetic eval result evaluated ``days_ago`` ago."""
    now = datetime.now(UTC)
    return CaseResult(
        case_slug=case,
        run_id=f"run-{case}-{days_ago}",
        passed=passed,
        evaluated_at=now - timedelta(days=days_ago),
    )


def test_window_stats_empty() -> None:
    stats = window_stats([], window_days=7)
    assert stats.total_runs == 0
    assert stats.pass_rate == 0.0


def test_window_stats_partial_pass() -> None:
    results = [make("c", days_ago=1, passed=True), make("c", days_ago=2, passed=False)]
    stats = window_stats(results, window_days=7)
    assert stats.total_runs == 2
    assert stats.passed_runs == 1
    assert stats.pass_rate == 0.5


def test_group_by_case() -> None:
    grouped = group_by_case(
        [
            make("a", days_ago=1, passed=True),
            make("a", days_ago=2, passed=False),
            make("b", days_ago=1, passed=True),
        ]
    )
    assert len(grouped["a"]) == 2
    assert len(grouped["b"]) == 1


def test_compute_drift_no_regression_when_baseline_too_small() -> None:
    now = datetime.now(UTC)
    results = [make("a", days_ago=1, passed=True) for _ in range(20)]  # all trailing
    report = compute_drift(results=results, now=now, threshold=0.15)
    assert report.insufficient_data is True
    assert report.regression is False


def test_compute_drift_detects_regression() -> None:
    now = datetime.now(UTC)
    baseline = [make("a", days_ago=20, passed=True) for _ in range(15)]  # 100% baseline
    trailing = [make("a", days_ago=2, passed=False) for _ in range(10)]  # 0% trailing
    report = compute_drift(results=baseline + trailing, now=now, threshold=0.15)
    assert report.insufficient_data is False
    assert report.regression is True
    assert report.overall_delta < -0.15
    assert any(d.case_slug == "a" for d in report.regressing_cases)


def test_compute_drift_no_regression_when_pass_rate_holds() -> None:
    now = datetime.now(UTC)
    baseline = [make("a", days_ago=20, passed=i % 2 == 0) for i in range(20)]  # 50%
    trailing = [make("a", days_ago=2, passed=i % 2 == 0) for i in range(10)]  # 50%
    report = compute_drift(results=baseline + trailing, now=now, threshold=0.15)
    assert report.regression is False


def test_handler_returns_envelope_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler should run cleanly with zero results — no regression, no SNS call."""
    sns_calls: list[dict[str, str]] = []

    def fake_load(**_kwargs: object) -> list[CaseResult]:
        return []

    def fake_persist(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_metric(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_publish(*_args: object, **_kwargs: object) -> None:
        sns_calls.append({"called": "true"})

    monkeypatch.setattr(h, "load_recent_results", fake_load)
    monkeypatch.setattr(h, "persist_report", fake_persist)
    monkeypatch.setattr(h, "emit_metric", fake_metric)
    monkeypatch.setattr(h, "publish_alert", fake_publish)

    out = h.handler({}, ctx())
    assert out["ok"] is True
    assert out["regression"] is False
    assert sns_calls == []  # no alert when no regression
