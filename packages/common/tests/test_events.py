"""Tests for ``common.events``."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from common.events import (
    CritiqueReady,
    EventEnvelope,
    IssueTriaged,
    RequestReceived,
    ReviewReady,
    RunCancelRequested,
    RunCompleted,
    TaskBlocked,
    TaskIterationRequested,
    TestReportReady,
)
from common.ids import new_correlation_id, new_event_id, new_run_id
from common.runtime import CiFailureFeedback, ReviewChangesRequestedFeedback


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


def test_request_received_workflow_kind_defaults_to_spec_driven() -> None:
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    assert payload.workflow_kind == "spec_driven"
    assert payload.synthetic_spec_slug is None


def test_request_received_carries_synthetic_spec_for_bug_fix() -> None:
    payload = RequestReceived(
        project_slug="demo",
        intent="x",
        requestor="triage",
        workflow_kind="bug_fix",
        synthetic_spec_slug="run-abc",
    )
    env = _env(payload)
    parsed = EventEnvelope[RequestReceived].model_validate_json(env.model_dump_json())
    assert parsed.payload.workflow_kind == "bug_fix"
    assert parsed.payload.synthetic_spec_slug == "run-abc"


def test_request_received_rejects_unknown_workflow_kind() -> None:
    with pytest.raises(ValidationError):
        RequestReceived.model_validate(
            {
                "project_slug": "demo",
                "intent": "x",
                "requestor": "alice",
                "workflow_kind": "other",
            },
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
    payload = RunCompleted(project_slug="demo", spec_slug="add-healthz", tasks_completed=3)
    rendered = json.loads(payload.model_dump_json())
    assert rendered == {"project_slug": "demo", "spec_slug": "add-healthz", "tasks_completed": 3}


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


def test_issue_triaged_proceed_validates() -> None:
    payload = IssueTriaged(
        project_slug="demo",
        target_repo="owner/name",
        issue_url="https://github.com/owner/name/issues/42",
        issue_number=42,
        action="proceed",
        workflow_kind="spec_driven",
        decision_s3_key="runs/r1/triage.json",
        rationale="Issue has clear acceptance criteria; routing to spec_driven.",
        confidence=0.92,
        session_id="r1-triage",
    )
    env = EventEnvelope[IssueTriaged](
        type="ISSUE.TRIAGED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="triage",
        payload=payload,
    )
    parsed = EventEnvelope[IssueTriaged].model_validate_json(env.model_dump_json())
    assert parsed.payload.action == "proceed"
    assert parsed.payload.workflow_kind == "spec_driven"


def test_issue_triaged_decline_no_workflow_kind() -> None:
    payload = IssueTriaged(
        project_slug="demo",
        target_repo="owner/name",
        issue_url="https://github.com/owner/name/issues/9",
        issue_number=9,
        action="decline",
        decision_s3_key="runs/r2/triage.json",
        rationale="Duplicate of #1.",
        session_id="r2-triage",
    )
    assert payload.workflow_kind is None


def test_issue_triaged_rejects_invalid_action() -> None:
    with pytest.raises(ValidationError):
        IssueTriaged.model_validate(
            {
                "project_slug": "x",
                "target_repo": "owner/name",
                "issue_url": "https://github.com/owner/name/issues/1",
                "issue_number": 1,
                "action": "yolo",
                "decision_s3_key": "runs/r/triage.json",
                "rationale": "x",
                "session_id": "x",
            },
        )


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


def test_task_iteration_requested_carries_ci_failure_feedback() -> None:
    payload = TaskIterationRequested(
        project_slug="demo",
        spec_slug="add-healthz",
        task_id="T-001",
        pr_url="https://github.com/x/y/pull/1",
        delivery_id="webhook-12345",
        feedback=CiFailureFeedback(
            workflow_name="ci",
            conclusion="failure",
            head_sha="abcdef0",
            html_url="https://github.com/x/y/actions/runs/1",
        ),
    )
    env = EventEnvelope[TaskIterationRequested](
        type="TASK.ITERATION_REQUESTED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="webhook",
        payload=payload,
    )
    parsed = EventEnvelope[TaskIterationRequested].model_validate_json(env.model_dump_json())
    assert parsed.payload.feedback.kind == "ci_failure"
    assert parsed.payload.delivery_id == "webhook-12345"


def test_task_iteration_requested_carries_review_feedback() -> None:
    payload = TaskIterationRequested(
        project_slug="demo",
        spec_slug="add-healthz",
        task_id="T-001",
        pr_url="https://github.com/x/y/pull/1",
        delivery_id="webhook-67890",
        feedback=ReviewChangesRequestedFeedback(
            reviewer="alice",
            body="please refactor the parser",
            review_id=42,
        ),
    )
    env = EventEnvelope[TaskIterationRequested](
        type="TASK.ITERATION_REQUESTED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="webhook",
        payload=payload,
    )
    parsed = EventEnvelope[TaskIterationRequested].model_validate_json(env.model_dump_json())
    assert parsed.payload.feedback.kind == "review_changes_requested"


def test_task_iteration_requested_rejects_unknown_feedback_kind() -> None:
    with pytest.raises(ValidationError):
        TaskIterationRequested.model_validate(
            {
                "project_slug": "demo",
                "spec_slug": "add-healthz",
                "task_id": "T-001",
                "pr_url": "https://github.com/x/y/pull/1",
                "delivery_id": "webhook-1",
                "feedback": {"kind": "unknown_kind"},
            },
        )


def test_run_cancel_requested_round_trip() -> None:
    payload = RunCancelRequested(
        project_slug="demo",
        requestor="alice",
        source="comment_command",
        reason="duplicate work",
    )
    env = EventEnvelope[RunCancelRequested](
        type="RUN.CANCEL_REQUESTED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="webhook",
        payload=payload,
    )
    parsed = EventEnvelope[RunCancelRequested].model_validate_json(env.model_dump_json())
    assert parsed.payload.source == "comment_command"
    assert parsed.payload.reason == "duplicate work"


def test_run_cancel_requested_optional_reason() -> None:
    payload = RunCancelRequested(
        project_slug="demo",
        requestor="github",
        source="issue_unassigned",
    )
    assert payload.reason is None


def test_run_cancel_requested_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        RunCancelRequested.model_validate(
            {
                "project_slug": "demo",
                "requestor": "alice",
                "source": "telepathy",
            },
        )


def test_task_blocked_round_trip() -> None:
    payload = TaskBlocked(
        project_slug="demo",
        spec_slug="add-healthz",
        task_id="T-001",
        pr_url="https://github.com/owner/name/pull/42",
        blocked_reason="Spec was contradictory.",
        session_id="01999999-9999-7999-9999-999999999999",
    )
    env = EventEnvelope[TaskBlocked](
        type="TASK.BLOCKED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="implementer",
        payload=payload,
    )
    parsed = EventEnvelope[TaskBlocked].model_validate_json(env.model_dump_json())
    assert parsed.type == "TASK.BLOCKED"
    assert parsed.payload.blocked_reason == "Spec was contradictory."
    assert parsed.payload.pr_url == "https://github.com/owner/name/pull/42"


def test_task_blocked_requires_blocked_reason() -> None:
    with pytest.raises(ValidationError):
        TaskBlocked.model_validate(
            {
                "project_slug": "demo",
                "spec_slug": "add-healthz",
                "task_id": "T-001",
                "pr_url": "https://github.com/owner/name/pull/42",
                "blocked_reason": "",
                "session_id": "01999999-9999-7999-9999-999999999999",
            },
        )
