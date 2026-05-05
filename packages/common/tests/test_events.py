"""Tests for ``common.events``."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from common.events import (
    CritiqueReady,
    EventEnvelope,
    RequestReceived,
    ReviewReady,
    RunCompleted,
    TestReportReady,
)
from common.ids import new_correlation_id, new_event_id, new_run_id


def _env(payload: RequestReceived) -> EventEnvelope[RequestReceived]:
    return EventEnvelope[RequestReceived](
        type="REQUEST.RECEIVED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="test",
        payload=payload,
    )


def test_round_trip_request_received() -> None:
    payload = RequestReceived(project_slug="demo", intent="add /healthz", requestor="alice")
    env = _env(payload)
    raw = env.model_dump_json()
    parsed = EventEnvelope[RequestReceived].model_validate_json(raw)
    assert parsed == env
    assert parsed.payload.project_slug == "demo"


def test_request_received_carries_source_issue_url() -> None:
    payload = RequestReceived(
        project_slug="demo",
        intent="add /healthz",
        requestor="triage",
        source_issue_url="https://github.com/owner/repo/issues/42",
    )
    env = _env(payload)
    raw = env.model_dump_json()
    parsed = EventEnvelope[RequestReceived].model_validate_json(raw)
    assert parsed.payload.source_issue_url == "https://github.com/owner/repo/issues/42"


def test_request_received_rejects_non_github_source_url() -> None:
    with pytest.raises(ValidationError):
        RequestReceived(
            project_slug="demo",
            intent="x",
            requestor="alice",
            source_issue_url="https://example.com/issues/1",
        )


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        RequestReceived.model_validate(
            {
                "project_slug": "demo",
                "intent": "x",
                "requestor": "alice",
                "extra_field": "should fail",
            },
        )


def test_run_completed_payload_has_required_fields() -> None:
    payload = RunCompleted(
        project_slug="demo",
        spec_slug="add-healthz",
        tasks_completed=3,
        total_duration_ms=12345,
        total_token_in=4096,
        total_token_out=2048,
        total_cost_usd=0.42,
    )
    rendered = json.loads(payload.model_dump_json())
    assert rendered["total_cost_usd"] == 0.42
    assert rendered["tasks_completed"] == 3


def test_envelope_type_is_literal_pinned() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    with pytest.raises(ValidationError):
        EventEnvelope[RequestReceived](
            type="NOT.A.REAL.TYPE",  # ty: ignore[invalid-argument-type]
            run_id=new_run_id(),
            correlation_id=new_correlation_id(),
            actor_id="t",
            payload=payload,
        )


def test_event_id_default_is_unique() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    a = _env(payload)
    b = _env(payload)
    assert a.event_id != b.event_id


def test_causation_id_optional() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    env = _env(payload).model_copy(update={"causation_id": new_event_id()})
    assert env.causation_id is not None


def test_round_trip_critique_ready() -> None:
    payload = CritiqueReady(
        project_slug="demo",
        spec_slug="add-healthz",
        critique_s3_key="runs/r1/critique.md",
        issue_count=3,
        high_severity_count=1,
        medium_severity_count=2,
        summary="Two ambiguous acceptance criteria; one missing failure mode.",
        session_id="r1-critic",
    )
    env = EventEnvelope[CritiqueReady](
        type="CRITIQUE.READY",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="critic",
        payload=payload,
    )
    parsed = EventEnvelope[CritiqueReady].model_validate_json(env.model_dump_json())
    assert parsed.payload.high_severity_count == 1
    assert parsed.payload.low_severity_count == 0


def test_review_ready_verdict_literal_pinned() -> None:
    with pytest.raises(ValidationError):
        ReviewReady(
            project_slug="demo",
            spec_slug="add-healthz",
            task_id="T-001",
            pr_url="https://github.com/x/y/pull/1",
            verdict="lgtm",  # ty: ignore[invalid-argument-type]
            comment_count=0,
            summary="x",
            session_id="r1-T-001-reviewer",
        )


def test_test_report_ready_round_trip() -> None:
    payload = TestReportReady(
        project_slug="demo",
        spec_slug="add-healthz",
        task_id="T-001",
        pr_url="https://github.com/x/y/pull/1",
        gap_count=2,
        suggested_test_count=4,
        summary="Missing tests for empty input + auth failure paths.",
        session_id="r1-T-001-tester",
    )
    env = EventEnvelope[TestReportReady](
        type="TEST_REPORT.READY",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="tester",
        payload=payload,
    )
    parsed = EventEnvelope[TestReportReady].model_validate_json(env.model_dump_json())
    assert parsed.payload.gap_count == 2
    assert parsed.payload.suggested_test_count == 4


def test_negative_severity_rejected() -> None:
    with pytest.raises(ValidationError):
        CritiqueReady(
            project_slug="demo",
            spec_slug="x",
            critique_s3_key="x",
            issue_count=0,
            high_severity_count=-1,
            summary="x",
            session_id="x",
        )
