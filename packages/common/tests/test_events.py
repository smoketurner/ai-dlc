"""Tests for ``common.events``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import common.events as events_module
from common.events import (
    ChecksFailed,
    ChecksPassed,
    CritiqueReady,
    DesignReady,
    EventEnvelope,
    ImplIterationRequested,
    ImplPrOpened,
    IssueTriaged,
    RequestReceived,
    ReviewReady,
    RunCancelRequested,
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


def test_request_received_drops_workflow_kind_and_synthetic_spec() -> None:
    """Pre-refactor fields must no longer exist on the payload."""
    payload = RequestReceived(project_slug="demo", intent="x", requestor="alice")
    assert not hasattr(payload, "workflow_kind")
    assert not hasattr(payload, "synthetic_spec_slug")


def test_request_received_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RequestReceived.model_validate(
            {
                "project_slug": "demo",
                "intent": "x",
                "requestor": "alice",
                "workflow_kind": "bug_fix",
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
    payload = RunCompleted(
        project_slug="demo",
        pr_url="https://github.com/x/y/pull/1",
    )
    assert payload.project_slug == "demo"
    assert payload.pr_url == "https://github.com/x/y/pull/1"


def test_run_completed_optional_pr_url() -> None:
    payload = RunCompleted(project_slug="demo")
    assert payload.pr_url is None


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


def test_design_ready_round_trip() -> None:
    payload = DesignReady(
        project_slug="demo",
        plan_s3_key="runs/r1/plan.md",
        summary="Add /healthz endpoint.",
        session_id="r1-architect",
        token_in=100,
        token_out=200,
        cost_usd=0.05,
        duration_ms=1500,
    )
    env = EventEnvelope[DesignReady](
        type="DESIGN.READY",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="architect",
        payload=payload,
    )
    parsed = EventEnvelope[DesignReady].model_validate_json(env.model_dump_json())
    assert parsed.payload.plan_s3_key == "runs/r1/plan.md"
    assert parsed.payload.token_in == 100


def test_round_trip_critique_ready() -> None:
    payload = CritiqueReady(
        project_slug="demo",
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


def test_impl_pr_opened_round_trip() -> None:
    payload = ImplPrOpened(
        project_slug="demo",
        pr_url="https://github.com/x/y/pull/42",
        diff_summary="Added /healthz route and tests.",
        session_id="r1-impl",
    )
    env = EventEnvelope[ImplPrOpened](
        type="IMPL_PR.OPENED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="implementer",
        payload=payload,
    )
    parsed = EventEnvelope[ImplPrOpened].model_validate_json(env.model_dump_json())
    assert parsed.payload.pr_url == "https://github.com/x/y/pull/42"


def test_impl_pr_opened_validates_pr_url_pattern() -> None:
    with pytest.raises(ValidationError):
        ImplPrOpened(
            project_slug="demo",
            pr_url="not-a-github-url",
            diff_summary="x",
            session_id="x",
        )


def test_impl_iteration_requested_round_trip() -> None:
    payload = ImplIterationRequested(
        project_slug="demo",
        pr_url="https://github.com/x/y/pull/1",
        delivery_id="webhook-12345",
        source="issue_comment_mention",
        commenter="alice",
        feedback_body="@aidlc-bot please refactor the parser",
    )
    env = EventEnvelope[ImplIterationRequested](
        type="IMPL.ITERATION_REQUESTED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="webhook",
        payload=payload,
    )
    parsed = EventEnvelope[ImplIterationRequested].model_validate_json(env.model_dump_json())
    assert parsed.payload.source == "issue_comment_mention"
    assert parsed.payload.delivery_id == "webhook-12345"


def test_impl_iteration_requested_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        ImplIterationRequested.model_validate(
            {
                "project_slug": "demo",
                "pr_url": "https://github.com/x/y/pull/1",
                "delivery_id": "id",
                "source": "telepathy",
                "commenter": "alice",
                "feedback_body": "x",
            },
        )


def test_checks_passed_round_trip() -> None:
    payload = ChecksPassed(
        project_slug="demo",
        pr_url="https://github.com/x/y/pull/1",
        head_sha="abcdef0",
        delivery_id="webhook-1",
    )
    env = EventEnvelope[ChecksPassed](
        type="CHECKS.PASSED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="webhook",
        payload=payload,
    )
    parsed = EventEnvelope[ChecksPassed].model_validate_json(env.model_dump_json())
    assert parsed.payload.head_sha == "abcdef0"


def test_checks_failed_round_trip() -> None:
    payload = ChecksFailed(
        project_slug="demo",
        pr_url="https://github.com/x/y/pull/1",
        head_sha="abcdef0",
        delivery_id="webhook-1",
        failed_workflow_count=2,
        summary="lint and test failed",
    )
    env = EventEnvelope[ChecksFailed](
        type="CHECKS.FAILED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="webhook",
        payload=payload,
    )
    parsed = EventEnvelope[ChecksFailed].model_validate_json(env.model_dump_json())
    assert parsed.payload.failed_workflow_count == 2


def test_checks_failed_requires_at_least_one_failure() -> None:
    with pytest.raises(ValidationError):
        ChecksFailed(
            project_slug="demo",
            pr_url="https://github.com/x/y/pull/1",
            head_sha="abcdef0",
            delivery_id="d",
            failed_workflow_count=0,
            summary="x",
        )


def test_review_ready_verdict_literal_pinned() -> None:
    with pytest.raises(ValidationError):
        ReviewReady(
            project_slug="demo",
            pr_url="https://github.com/x/y/pull/1",
            verdict="lgtm",  # ty: ignore[invalid-argument-type]
            comment_count=0,
            summary="x",
            session_id="r1-reviewer",
        )


def test_test_report_ready_round_trip() -> None:
    payload = TestReportReady(
        project_slug="demo",
        pr_url="https://github.com/x/y/pull/1",
        gap_count=2,
        suggested_test_count=4,
        summary="Missing tests for empty input + auth failure paths.",
        session_id="r1-tester",
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
        decision_s3_key="runs/r1/triage.json",
        rationale="Issue has clear acceptance criteria.",
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


def test_issue_triaged_research_validates() -> None:
    payload = IssueTriaged(
        project_slug="demo",
        target_repo="owner/name",
        issue_url="https://github.com/owner/name/issues/42",
        issue_number=42,
        action="research",
        decision_s3_key="runs/r1/triage.json",
        rationale="Issue body links three RFCs.",
        session_id="r1-triage",
    )
    assert payload.action == "research"


def test_issue_triaged_decline_validates() -> None:
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
    assert payload.action == "decline"


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


def test_issue_triaged_rejects_workflow_kind() -> None:
    """workflow_kind was removed — it must be rejected as an extra field."""
    with pytest.raises(ValidationError):
        IssueTriaged.model_validate(
            {
                "project_slug": "x",
                "target_repo": "owner/name",
                "issue_url": "https://github.com/owner/name/issues/1",
                "issue_number": 1,
                "action": "proceed",
                "workflow_kind": "spec_driven",
                "decision_s3_key": "runs/r/triage.json",
                "rationale": "x",
                "session_id": "x",
            },
        )


def test_negative_severity_rejected() -> None:
    with pytest.raises(ValidationError):
        CritiqueReady(
            project_slug="demo",
            critique_s3_key="x",
            issue_count=0,
            high_severity_count=-1,
            summary="x",
            session_id="x",
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


def test_deleted_payloads_not_exported() -> None:
    """Spec/task-era payload classes were removed in the refactor."""
    for removed in (
        "SpecReady",
        "SpecApproved",
        "SpecRejected",
        "SpecIterationRequested",
        "TaskReady",
        "TaskApproved",
        "TaskRejected",
        "TaskBlocked",
        "TaskIterationRequested",
    ):
        assert not hasattr(events_module, removed), f"{removed} should be removed"
