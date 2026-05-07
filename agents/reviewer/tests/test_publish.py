"""Tests for the no-task-token publish path in ``reviewer.app``.

When the iteration_reactor invokes the reviewer (no SF task_token), the
agent must publish ``REVIEW.READY`` itself before returning so downstream
consumers see the completion. SFN-driven invocations stay unchanged.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from common.events import EventEnvelope, ReviewReady
from common.runtime import ReviewerInput, ReviewerResult
from reviewer.app import publish_review_ready


def make_input(*, task_token: str | None = None) -> ReviewerInput:
    return ReviewerInput.model_validate(
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


def make_result() -> ReviewerResult:
    return ReviewerResult(
        task_id="T-001",
        pr_url="https://github.com/x/y/pull/1",
        verdict="request_changes",
        comment_count=2,
        high_severity_count=1,
        medium_severity_count=1,
        summary="Liveness check is too shallow.",
        session_id="run-T-001-reviewer",
        token_in=2_000,
        token_out=400,
        cost_usd=0.012,
        duration_ms=22_000,
    )


def test_publish_review_ready_builds_correct_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[EventEnvelope[Any]] = []

    def fake_publish(envelope: EventEnvelope[Any]) -> None:
        captured.append(envelope)

    monkeypatch.setattr("reviewer.app.publish", fake_publish)
    payload = make_input()
    result = make_result()

    publish_review_ready(payload, result)

    assert len(captured) == 1
    env = captured[0]
    assert env.type == "REVIEW.READY"
    assert env.actor_id == "reviewer"
    assert env.run_id == payload.run_id
    assert env.correlation_id == payload.correlation_id
    assert isinstance(env.payload, ReviewReady)
    assert env.payload.project_slug == "demo"
    assert env.payload.spec_slug == "add-healthz"
    assert env.payload.task_id == "T-001"
    assert env.payload.verdict == "request_changes"
    assert env.payload.high_severity_count == 1
    assert env.payload.token_in == 2_000


def test_publish_review_ready_envelope_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch any field shape drift from ReviewerResult → ReviewReady."""
    captured: list[EventEnvelope[ReviewReady]] = []
    monkeypatch.setattr("reviewer.app.publish", captured.append)

    publish_review_ready(make_input(), make_result())

    raw = captured[0].model_dump_json()
    parsed = EventEnvelope[ReviewReady].model_validate_json(raw)
    assert parsed.payload.summary == "Liveness check is too shallow."
