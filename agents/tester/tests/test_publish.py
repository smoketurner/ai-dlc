"""Tests for the no-task-token publish path in ``tester.app``.

When the iteration_reactor invokes the tester (no SF task_token), the
agent must publish ``TEST_REPORT.READY`` itself before returning so
downstream consumers see the completion. SFN-driven invocations stay
unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

# Aliased imports keep pytest from trying to collect the ``Test*``-prefixed
# pydantic models as test classes (a harmless but noisy collection warning).
from common.events import EventEnvelope
from common.events import TestReportReady as TestReportReadyPayload
from common.runtime import TesterInput as RuntimeInput
from common.runtime import TesterResult as RuntimeResult
from tester.app import publish_test_report_ready


def make_input(*, task_token: str | None = None) -> RuntimeInput:
    return RuntimeInput.model_validate(
        {
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "spec_s3_prefix": "specs/add-healthz/",
            "task_id": "T-001",
            "pr_url": "https://github.com/x/y/pull/1",
            "diff_summary": "Adds /healthz route.",
            "run_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "task_token": task_token,
        },
    )


def make_result() -> RuntimeResult:
    return RuntimeResult(
        task_id="T-001",
        pr_url="https://github.com/x/y/pull/1",
        gap_count=2,
        suggested_test_count=4,
        summary="Missing tests for empty input + auth failure.",
        session_id="run-T-001-tester",
        token_in=1_500,
        token_out=300,
        cost_usd=0.005,
        duration_ms=18_000,
    )


def test_publish_test_report_ready_builds_correct_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[EventEnvelope[Any]] = []
    monkeypatch.setattr("tester.app.publish", captured.append)

    payload = make_input()
    result = make_result()

    publish_test_report_ready(payload, result)

    assert len(captured) == 1
    env = captured[0]
    assert env.type == "TEST_REPORT.READY"
    assert env.actor_id == "tester"
    assert env.run_id == payload.run_id
    assert env.correlation_id == payload.correlation_id
    assert isinstance(env.payload, TestReportReadyPayload)
    assert env.payload.gap_count == 2
    assert env.payload.suggested_test_count == 4
    assert env.payload.token_in == 1_500


def test_publish_test_report_ready_envelope_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch any field shape drift from runtime result -> bus payload."""
    captured: list[EventEnvelope[TestReportReadyPayload]] = []
    monkeypatch.setattr("tester.app.publish", captured.append)

    publish_test_report_ready(make_input(), make_result())

    raw = captured[0].model_dump_json()
    parsed = EventEnvelope[TestReportReadyPayload].model_validate_json(raw)
    assert parsed.payload.summary == "Missing tests for empty input + auth failure."
