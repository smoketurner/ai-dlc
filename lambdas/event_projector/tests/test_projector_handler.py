"""Unit tests for event_projector — moto-backed runs table; agentcore is mocked."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from event_projector.handler import agentcore, ddb, handler
from moto import mock_aws

TABLE = "ai-dlc-test-runs"
MEM = "MEMORY-XYZ"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in for powertools."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="event_projector-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
            get_remaining_time_in_millis=lambda: 30_000,
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Set env vars, mock agentcore client, create the runs table under moto.

    Yields the agentcore MagicMock so tests that need to inspect
    ``create_event`` calls can ``def test_foo(aws_env: MagicMock)``.
    """
    monkeypatch.setenv("AIDLC_RUNS_TABLE", TABLE)
    monkeypatch.setenv("AIDLC_MEMORY_ID", MEM)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    ddb.cache_clear()
    agentcore.cache_clear()

    fake_agentcore = MagicMock()
    monkeypatch.setattr("event_projector.handler.agentcore", lambda: fake_agentcore)

    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield fake_agentcore
    ddb.cache_clear()
    agentcore.cache_clear()


def query_outbox(run_id: str) -> list[dict[str, Any]]:
    """Return all OUTBOX# rows for a run, freshest order doesn't matter."""
    items = ddb().query(
        TableName=TABLE,
        KeyConditionExpression="pk = :p AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":p": {"S": f"RUN#{run_id}"}, ":prefix": {"S": "OUTBOX#"}},
    )["Items"]
    return list(items)


def state_of(run_id: str) -> dict[str, Any]:
    """Helper: return the STATE row for ``run_id`` as a raw DDB item."""
    return ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
    )["Item"]


def envelope(**overrides: Any) -> dict[str, Any]:
    """Build a canonical envelope; overrides win field-by-field."""
    base = {
        "schema_version": "1.0",
        "event_id": "01J0000000000000000000000A",
        "type": "DESIGN.READY",
        "timestamp": "2026-05-01T12:00:00Z",
        "run_id": "run-1",
        "correlation_id": "cor-1",
        "actor_id": "system",
        "payload": {
            "project_slug": "demo",
            "plan_s3_key": "runs/run-1/plan.md",
            "summary": "x",
            "session_id": "run-1",
        },
    }
    base.update(overrides)
    return base


def eb_event(env: dict[str, Any]) -> dict[str, Any]:
    """Wrap an envelope in an EventBridge event shape."""
    return {
        "version": "0",
        "id": "11111111-2222-3333-4444-555555555555",
        "detail-type": env["type"],
        "source": "ai-dlc.system",
        "account": "000000000000",
        "time": "2026-05-01T12:00:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail": env,
    }


# ---------------------------------------------------------------------------
# Basic projection + REQUEST.RECEIVED
# ---------------------------------------------------------------------------


def test_eventbridge_event_writes_event_row() -> None:
    out = handler(eb_event(envelope()), ctx())
    assert out["ok"] is True
    items = ddb().query(
        TableName=TABLE,
        KeyConditionExpression="pk = :p AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":p": {"S": "RUN#run-1"}, ":prefix": {"S": "EVENT#"}},
    )["Items"]
    assert len(items) == 1
    assert items[0]["type"]["S"] == "DESIGN.READY"


def test_request_received_writes_current_state_received() -> None:
    """REQUEST.RECEIVED on a fresh run sets ``current_state=received``."""
    received = envelope(
        type="REQUEST.RECEIVED",
        payload={
            "project_slug": "demo",
            "intent": "Add /version endpoint",
            "requestor": "alice",
        },
    )
    handler(eb_event(received), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "received"
    assert state["last_event_id"]["S"] == "01J0000000000000000000000A"
    assert state["state_transitions"]["N"] == "1"


def test_request_received_projects_source_issue_fields() -> None:
    """REQUEST.RECEIVED persists source-issue url/title/body + GSI keys."""
    received = envelope(
        type="REQUEST.RECEIVED",
        payload={
            "project_slug": "demo",
            "intent": "x",
            "requestor": "alice",
            "source_issue_url": "https://github.com/o/r/issues/7",
            "source_issue_title": "fix flaky test",
            "source_issue_body": "ci has been flaky for a week",
        },
    )

    handler(eb_event(received), ctx())

    state = state_of("run-1")
    assert state["gsi1pk"]["S"] == "ISSUE#https://github.com/o/r/issues/7"
    assert state["gsi1sk"]["S"] == "RUN#run-1"
    assert state["source_issue_url"]["S"] == "https://github.com/o/r/issues/7"
    assert state["source_issue_title"]["S"] == "fix flaky test"
    assert state["source_issue_body"]["S"] == "ci has been flaky for a week"


def test_request_received_without_issue_url_skips_index() -> None:
    received = envelope(
        type="REQUEST.RECEIVED",
        payload={
            "project_slug": "demo",
            "intent": "Manual run via dashboard",
            "requestor": "alice",
        },
    )

    handler(eb_event(received), ctx())

    state = state_of("run-1")
    assert "gsi1pk" not in state
    assert "gsi1sk" not in state


def test_redelivered_event_completes_projection() -> None:
    """Redelivery is idempotent — single state transition and one EVENT row."""
    received = envelope(
        type="REQUEST.RECEIVED",
        payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
    )
    handler(eb_event(received), ctx())
    out = handler(eb_event(received), ctx())
    assert out["ok"] is True
    assert out["committed"] is False
    state = state_of("run-1")
    assert state["current_state"]["S"] == "received"
    assert state["state_transitions"]["N"] == "1"


def test_two_request_received_events_only_transition_once() -> None:
    """A second REQUEST.RECEIVED with a different event_id no-ops the transition."""
    first = envelope(
        type="REQUEST.RECEIVED",
        event_id="01J0000000000000000000000A",
        payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
    )
    second = envelope(
        type="REQUEST.RECEIVED",
        event_id="01J0000000000000000000000B",
        payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
    )
    handler(eb_event(first), ctx())
    handler(eb_event(second), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "received"
    assert state["state_transitions"]["N"] == "1"


# ---------------------------------------------------------------------------
# ISSUE.TRIAGED
# ---------------------------------------------------------------------------


def test_issue_triaged_projects_action_and_decision_key() -> None:
    """ISSUE.TRIAGED persists triage_action + decision_s3_key on the STATE row."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "triaging"},
        },
    )
    triaged = envelope(
        type="ISSUE.TRIAGED",
        payload={
            "project_slug": "demo",
            "target_repo": "o/r",
            "issue_url": "https://github.com/o/r/issues/7",
            "issue_number": 7,
            "action": "proceed",
            "decision_s3_key": "runs/run-1/triage.json",
            "rationale": "ok",
            "session_id": "run-1-triage",
        },
    )
    handler(eb_event(triaged), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "triage_decided"
    assert state["triage_action"]["S"] == "proceed"
    assert state["decision_s3_key"]["S"] == "runs/run-1/triage.json"


# ---------------------------------------------------------------------------
# DESIGN.READY / CRITIQUE.READY (internal artifacts)
# ---------------------------------------------------------------------------


def test_design_ready_advances_state_to_designed() -> None:
    """DESIGN.READY arriving in architect_running advances to designed."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
        },
    )
    handler(eb_event(envelope(type="DESIGN.READY")), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "designed"
    assert state["plan_s3_key"]["S"] == "runs/run-1/plan.md"


def test_design_ready_in_wrong_state_is_dropped() -> None:
    """DESIGN.READY arriving in the wrong state leaves the cursor in place."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "received"},
        },
    )
    handler(eb_event(envelope(type="DESIGN.READY")), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "received"


def test_critique_ready_projects_severity_counts() -> None:
    """CRITIQUE.READY from critic_running advances state + records counts."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "critic_running"},
        },
    )
    critique = envelope(
        type="CRITIQUE.READY",
        payload={
            "project_slug": "demo",
            "critique_s3_key": "runs/run-1/critique.md",
            "issue_count": 3,
            "high_severity_count": 1,
            "medium_severity_count": 1,
            "low_severity_count": 1,
            "summary": "moderate",
            "session_id": "run-1-critic",
        },
    )
    handler(eb_event(critique), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "critiqued"
    assert state["critique_s3_key"]["S"] == "runs/run-1/critique.md"
    assert state["critique_high_severity_count"]["N"] == "1"
    assert state["critique_medium_severity_count"]["N"] == "1"
    assert state["critique_low_severity_count"]["N"] == "1"


# ---------------------------------------------------------------------------
# IMPL_PR.OPENED
# ---------------------------------------------------------------------------


def test_impl_pr_opened_advances_to_impl_pr_open() -> None:
    """IMPL_PR.OPENED from implementer_running advances + writes pr_url + gsi_pr."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "implementer_running"},
        },
    )
    impl_pr = envelope(
        type="IMPL_PR.OPENED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "diff_summary": "added stuff",
            "session_id": "run-1-impl",
        },
    )
    handler(eb_event(impl_pr), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "impl_pr_open"
    assert state["pr_url"]["S"] == "https://github.com/o/r/pull/42"
    assert state["gsi_pr"]["S"] == "PR#https://github.com/o/r/pull/42"


# ---------------------------------------------------------------------------
# Validators: REVIEW.READY, TEST_REPORT.READY, CODE_CRITIQUE.READY
# ---------------------------------------------------------------------------


def test_review_ready_records_verdict_and_advances() -> None:
    """REVIEW.READY from validation_running → validation_complete; verdict stuck."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "validation_running"},
        },
    )
    review = envelope(
        type="REVIEW.READY",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "verdict": "approve",
            "comment_count": 0,
            "high_severity_count": 0,
            "medium_severity_count": 1,
            "low_severity_count": 0,
            "summary": "lgtm",
            "session_id": "run-1-reviewer",
        },
    )
    handler(eb_event(review), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "validation_complete"
    assert state["reviewer_verdict"]["S"] == "approve"
    assert state["reviewer_medium_severity_count"]["N"] == "1"


def test_test_report_ready_records_counters_no_state_advance() -> None:
    """TEST_REPORT.READY is advisory — counters + status, no transition."""
    test_report = envelope(
        type="TEST_REPORT.READY",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "gap_count": 4,
            "suggested_test_count": 7,
            "summary": "we need more tests",
            "session_id": "run-1-tester",
        },
    )
    handler(eb_event(test_report), ctx())
    state = state_of("run-1")
    assert state["status"]["S"] == "TEST_REPORT.READY"
    assert state["tester_gap_count"]["N"] == "4"
    assert state["suggested_test_count"]["N"] == "7"
    assert query_outbox("run-1") == []


def test_code_critique_ready_projects_artifact_key_and_counts() -> None:
    """CODE_CRITIQUE.READY is advisory — records artefact + severity counts."""
    cc = envelope(
        type="CODE_CRITIQUE.READY",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "critique_s3_key": "runs/run-1/code-critique.md",
            "issue_count": 2,
            "high_severity_count": 2,
            "summary": "",
            "session_id": "run-1-code-critic",
        },
    )
    handler(eb_event(cc), ctx())
    state = state_of("run-1")
    assert state["code_critic_critique_s3_key"]["S"] == "runs/run-1/code-critique.md"
    assert state["code_critic_high_severity_count"]["N"] == "2"
    assert query_outbox("run-1") == []


# ---------------------------------------------------------------------------
# REVISION.READY
# ---------------------------------------------------------------------------


def test_revision_ready_advances_and_records_revision_number() -> None:
    """REVISION.READY from revising → validation_running; revision_count set."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "revising"},
        },
    )
    revision = envelope(
        type="REVISION.READY",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "diff_summary": "fixed feedback",
            "revision_number": 2,
            "session_id": "run-1-impl-r2",
        },
    )
    handler(eb_event(revision), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "validation_running"
    assert state["revision_count"]["N"] == "2"


# ---------------------------------------------------------------------------
# CHECKS.PASSED / CHECKS.FAILED
# ---------------------------------------------------------------------------


def test_checks_passed_in_validation_complete_advances_to_human_merge() -> None:
    """CHECKS.PASSED from validation_complete → awaiting_human_merge."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "validation_complete"},
        },
    )
    checks = envelope(
        type="CHECKS.PASSED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "head_sha": "abcdef0",
            "delivery_id": "del-1",
        },
    )
    handler(eb_event(checks), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "awaiting_human_merge"
    assert state["check_state"]["S"] == "passed"
    assert state["check_head_sha"]["S"] == "abcdef0"


def test_checks_passed_in_awaiting_checks_advances_to_human_merge() -> None:
    """CHECKS.PASSED arriving in awaiting_checks also advances to human-merge."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "awaiting_checks"},
        },
    )
    checks = envelope(
        type="CHECKS.PASSED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "head_sha": "abcdef0",
            "delivery_id": "del-1",
        },
    )
    handler(eb_event(checks), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "awaiting_human_merge"


def test_checks_failed_advances_to_revising_and_queues_feedback() -> None:
    """CHECKS.FAILED advances to revising + appends ci_failure FeedbackItem."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "validation_complete"},
        },
    )
    checks = envelope(
        type="CHECKS.FAILED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "head_sha": "abcdef0",
            "delivery_id": "del-fail-1",
            "failed_workflow_count": 2,
            "summary": "https://github.com/o/r/actions/runs/9",
        },
    )
    handler(eb_event(checks), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "revising"
    assert state["check_state"]["S"] == "failed"
    assert state["check_head_sha"]["S"] == "abcdef0"
    assert state["last_revision_trigger"]["S"] == "ci_failure"
    assert len(state["pending_revision_feedback"]["L"]) == 1
    item = state["pending_revision_feedback"]["L"][0]["M"]
    assert item["kind"]["S"] == "ci_failure"
    assert item["head_sha"]["S"] == "abcdef0"
    assert item["conclusion"]["S"] == "failure"
    assert state["delivery_ids"]["SS"] == ["del-fail-1"]


# ---------------------------------------------------------------------------
# IMPL.ITERATION_REQUESTED (human mention feedback)
# ---------------------------------------------------------------------------


def test_impl_iteration_requested_issue_comment_appends_mention_feedback() -> None:
    """A human @-mention from an issue comment queues a mention FeedbackItem.

    From ``impl_pr_open`` the cursor advances to ``revising`` and the
    feedback lands on ``pending_revision_feedback``.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "impl_pr_open"},
        },
    )
    mention = envelope(
        type="IMPL.ITERATION_REQUESTED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "delivery_id": "webhook-1",
            "source": "issue_comment_mention",
            "commenter": "alice",
            "feedback_body": "please also rename Foo",
        },
    )
    handler(eb_event(mention), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "revising"
    assert state["last_revision_trigger"]["S"] == "human_mention"
    assert state["delivery_ids"]["SS"] == ["webhook-1"]
    items = state["pending_revision_feedback"]["L"]
    assert len(items) == 1
    entry = items[0]["M"]
    assert entry["kind"]["S"] == "issue_comment_mention"
    assert entry["body"]["S"] == "please also rename Foo"
    assert entry["commenter"]["S"] == "alice"


def test_impl_iteration_requested_review_comment_uses_review_variant() -> None:
    """``source=review_comment_mention`` → review_comment_mention FeedbackItem."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "impl_pr_open"},
        },
    )
    mention = envelope(
        type="IMPL.ITERATION_REQUESTED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "delivery_id": "webhook-2",
            "source": "review_comment_mention",
            "commenter": "alice",
            "feedback_body": "this loop is N+1",
        },
    )
    handler(eb_event(mention), ctx())
    items = state_of("run-1")["pending_revision_feedback"]["L"]
    assert items[0]["M"]["kind"]["S"] == "review_comment_mention"


def test_impl_iteration_requested_review_changes_uses_changes_variant() -> None:
    """``source=review_changes_requested`` → review_changes_requested FeedbackItem."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "impl_pr_open"},
        },
    )
    mention = envelope(
        type="IMPL.ITERATION_REQUESTED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "delivery_id": "webhook-3",
            "source": "review_changes_requested",
            "commenter": "alice",
            "feedback_body": "needs more tests",
        },
    )
    handler(eb_event(mention), ctx())
    entry = state_of("run-1")["pending_revision_feedback"]["L"][0]["M"]
    assert entry["kind"]["S"] == "review_changes_requested"
    assert entry["reviewer"]["S"] == "alice"
    assert entry["body"]["S"] == "needs more tests"


def test_second_impl_iteration_in_revising_appends_without_double_advance() -> None:
    """A second mention while already revising appends to the queue.

    The cursor stays in ``revising`` (no transition for revising →
    revising), but the feedback item must still land on the queue so
    the in-flight revision picks it up.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "revising"},
            "delivery_ids": {"SS": ["webhook-1"]},
            "pending_revision_feedback": {
                "L": [
                    {
                        "M": {
                            "kind": {"S": "issue_comment_mention"},
                            "comment_id": {"N": "0"},
                            "body": {"S": "first one"},
                            "commenter": {"S": "alice"},
                        },
                    },
                ],
            },
        },
    )
    second = envelope(
        type="IMPL.ITERATION_REQUESTED",
        event_id="01J0000000000000000000000B",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "delivery_id": "webhook-2",
            "source": "issue_comment_mention",
            "commenter": "alice",
            "feedback_body": "also rename Foo",
        },
    )
    handler(eb_event(second), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "revising"
    assert sorted(state["delivery_ids"]["SS"]) == ["webhook-1", "webhook-2"]
    assert len(state["pending_revision_feedback"]["L"]) == 2


# ---------------------------------------------------------------------------
# RUN.COMPLETED / RUN.FAILED / RUN.CANCEL_REQUESTED — terminal projections
# ---------------------------------------------------------------------------


def test_run_completed_advances_to_done_from_awaiting_human_merge() -> None:
    """RUN.COMPLETED from awaiting_human_merge → done."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "awaiting_human_merge"},
        },
    )
    completed = envelope(
        type="RUN.COMPLETED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
        },
    )
    handler(eb_event(completed), ctx())
    state = state_of("run-1")
    assert state["status"]["S"] == "RUN.COMPLETED"
    assert state["current_state"]["S"] == "done"
    assert len(query_outbox("run-1")) == 1


def test_run_failed_advances_any_non_terminal_to_failed() -> None:
    """RUN.FAILED short-circuits any non-terminal state into failed."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "implementer_running"},
        },
    )
    failed = envelope(
        type="RUN.FAILED",
        payload={
            "project_slug": "demo",
            "failed_state": "implementer_running",
            "error_class": "Timeout",
            "error_message": "agent stalled",
            "retryable": False,
        },
    )
    handler(eb_event(failed), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "failed"


def test_run_cancel_requested_advances_to_cancelled() -> None:
    """RUN.CANCEL_REQUESTED short-circuits any non-terminal state to cancelled."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "validation_running"},
        },
    )
    cancel = envelope(
        type="RUN.CANCEL_REQUESTED",
        payload={
            "project_slug": "demo",
            "requestor": "alice",
            "source": "dashboard",
        },
    )
    handler(eb_event(cancel), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "cancelled"


# ---------------------------------------------------------------------------
# Usage accumulation
# ---------------------------------------------------------------------------


def test_per_event_usage_accumulates_on_state_row() -> None:
    """Each *.READY event with token/cost fields ADDs to running totals."""
    design = envelope(
        type="DESIGN.READY",
        event_id="01J0000000000000000000000B",
        payload={
            "project_slug": "demo",
            "plan_s3_key": "runs/run-1/plan.md",
            "summary": "x",
            "token_in": 4_000,
            "token_out": 1_500,
            "cost_usd": 0.25,
            "duration_ms": 30_000,
            "session_id": "run-1-arch",
        },
    )
    review = envelope(
        type="REVIEW.READY",
        event_id="01J0000000000000000000000C",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "verdict": "approve",
            "comment_count": 0,
            "summary": "",
            "token_in": 1_000,
            "token_out": 500,
            "cost_usd": 0.05,
            "duration_ms": 8_000,
            "session_id": "run-1-reviewer",
        },
    )

    handler(eb_event(design), ctx())
    handler(eb_event(review), ctx())

    state = state_of("run-1")
    assert state["total_token_in"]["N"] == "5000"
    assert state["total_token_out"]["N"] == "2000"
    assert float(state["total_cost_usd"]["N"]) == pytest.approx(0.30)
    assert state["total_duration_ms"]["N"] == "38000"


def test_event_with_zero_usage_skips_add() -> None:
    """Zero usage skips the ADD clause to avoid a no-op DDB write."""
    design = envelope(
        type="DESIGN.READY",
        payload={
            "project_slug": "demo",
            "plan_s3_key": "runs/run-1/plan.md",
            "summary": "",
            "token_in": 0,
            "token_out": 0,
            "cost_usd": 0.0,
            "duration_ms": 0,
            "session_id": "run-1-arch",
        },
    )
    handler(eb_event(design), ctx())
    state = state_of("run-1")
    assert "total_token_in" not in state
    assert "total_cost_usd" not in state


def test_redelivered_state_advance_does_not_double_count_usage() -> None:
    """A re-delivered DESIGN.READY rolls the whole transaction back."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
        },
    )
    design = envelope(
        type="DESIGN.READY",
        payload={
            "project_slug": "demo",
            "plan_s3_key": "runs/run-1/plan.md",
            "summary": "",
            "token_in": 4_000,
            "token_out": 1_500,
            "cost_usd": 0.25,
            "duration_ms": 30_000,
            "session_id": "run-1-arch",
        },
    )
    handler(eb_event(design), ctx())
    handler(eb_event(design), ctx())
    state = state_of("run-1")
    assert state["total_token_in"]["N"] == "4000"
    assert state["total_token_out"]["N"] == "1500"
    assert float(state["total_cost_usd"]["N"]) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Idempotency + memory forwarding
# ---------------------------------------------------------------------------


def test_duplicate_event_id_silently_skipped() -> None:
    """Redelivery returns ok and doesn't double-write the EVENT row."""
    env = envelope()
    handler(eb_event(env), ctx())
    out = handler(eb_event(env), ctx())
    assert out["ok"] is True
    assert out["committed"] is False
    items = ddb().query(
        TableName=TABLE,
        KeyConditionExpression="pk = :p AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":p": {"S": "RUN#run-1"}, ":prefix": {"S": "EVENT#"}},
    )["Items"]
    assert len(items) == 1


def test_redelivered_event_does_not_double_emit_memory(aws_env: MagicMock) -> None:
    """Memory CreateEvent is gated on the transaction committing."""
    env = envelope()
    handler(eb_event(env), ctx())
    handler(eb_event(env), ctx())
    aws_env.create_event.assert_called_once()


def test_unknown_trigger_returns_error() -> None:
    out = handler({"foo": "bar"}, ctx())
    assert out["ok"] is False


def test_eventbridge_with_string_detail() -> None:
    """Real EventBridge ships ``detail`` as a JSON string sometimes."""
    env = envelope()
    payload = eb_event(env)
    payload["detail"] = json.dumps(env)
    out = handler(payload, ctx())
    assert out["ok"] is True


def test_forward_to_memory_calls_create_event_with_required_shape(aws_env: MagicMock) -> None:
    """``CreateEvent`` requires eventTimestamp + tagged-union payload."""
    handler(eb_event(envelope()), ctx())
    aws_env.create_event.assert_called_once()
    kwargs = aws_env.create_event.call_args.kwargs
    assert kwargs["memoryId"] == MEM
    assert kwargs["actorId"] == "demo"
    assert kwargs["sessionId"] == "run-1"
    assert kwargs["eventTimestamp"] == datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    assert len(kwargs["payload"]) == 1
    entry = kwargs["payload"][0]
    assert set(entry.keys()) == {"blob"}
    assert isinstance(entry["blob"], dict)
    assert entry["blob"]["type"] == "DESIGN.READY"
    assert entry["blob"]["run_id"] == "run-1"


def test_forward_to_memory_swallows_botocore_error(aws_env: MagicMock) -> None:
    """Memory CreateEvent errors are warning-logged, not raised."""
    aws_env.create_event.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "memory unavailable"}},
        "CreateEvent",
    )
    out = handler(eb_event(envelope()), ctx())
    assert out["ok"] is True
    assert out["committed"] is True


# ---------------------------------------------------------------------------
# Dispatch circuit-breaker counter reset
# ---------------------------------------------------------------------------


def test_design_ready_resets_run_dispatch_failure_count() -> None:
    """A successful architect dispatch zeroes the run-row breaker counter."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
            "dispatch_failure_count": {"N": "2"},
        },
    )
    handler(eb_event(envelope(type="DESIGN.READY")), ctx())
    state = state_of("run-1")
    assert state["current_state"]["S"] == "designed"
    assert state["dispatch_failure_count"]["N"] == "0"


def test_critique_ready_resets_run_dispatch_failure_count() -> None:
    """CRITIQUE.READY closes the breaker on the run row."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "critic_running"},
            "dispatch_failure_count": {"N": "1"},
        },
    )
    critique = envelope(
        type="CRITIQUE.READY",
        payload={
            "project_slug": "demo",
            "critique_s3_key": "runs/run-1/critique.md",
            "issue_count": 0,
            "summary": "no issues",
            "session_id": "run-1-critic",
        },
    )
    handler(eb_event(critique), ctx())
    state = state_of("run-1")
    assert state["dispatch_failure_count"]["N"] == "0"


def test_impl_pr_opened_resets_run_dispatch_failure_count() -> None:
    """IMPL_PR.OPENED is also a successful dispatch (the implementer ran)."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "implementer_running"},
            "dispatch_failure_count": {"N": "3"},
        },
    )
    impl = envelope(
        type="IMPL_PR.OPENED",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "diff_summary": "x",
            "session_id": "run-1-impl",
        },
    )
    handler(eb_event(impl), ctx())
    state = state_of("run-1")
    assert state["dispatch_failure_count"]["N"] == "0"


def test_review_ready_does_not_touch_dispatch_failure_count() -> None:
    """REVIEW.READY is gated by GuardedAdvance — counter is untouched here."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "validation_running"},
            "dispatch_failure_count": {"N": "2"},
        },
    )
    review = envelope(
        type="REVIEW.READY",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "verdict": "approve",
            "comment_count": 0,
            "summary": "",
            "session_id": "run-1-reviewer",
        },
    )
    handler(eb_event(review), ctx())
    state = state_of("run-1")
    assert state["dispatch_failure_count"]["N"] == "2"


# ---------------------------------------------------------------------------
# Outbox row written atomically with state advance
# ---------------------------------------------------------------------------


def test_run_state_advance_writes_outbox_row() -> None:
    """Successful run-state advance writes one OUTBOX# row in the same transaction."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
        },
    )
    handler(eb_event(envelope(type="DESIGN.READY")), ctx())
    rows = query_outbox("run-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["sk"]["S"] == "OUTBOX#01J0000000000000000000000A"
    assert row["run_id"]["S"] == "run-1"
    assert row["project_slug"]["S"] == "demo"
    assert int(row["expire_at"]["N"]) > 0


def test_advisory_event_does_not_write_outbox_row() -> None:
    """TEST_REPORT.READY updates side data only — no state advance, no outbox."""
    test_report = envelope(
        type="TEST_REPORT.READY",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/42",
            "gap_count": 1,
            "suggested_test_count": 2,
            "summary": "",
            "session_id": "run-1-tester",
        },
    )
    handler(eb_event(test_report), ctx())
    assert query_outbox("run-1") == []


def test_idempotent_redelivery_writes_outbox_only_once() -> None:
    """Re-delivered state-advance event: first commits, second is a CCFE no-op."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
        },
    )
    design = envelope(type="DESIGN.READY")
    handler(eb_event(design), ctx())
    handler(eb_event(design), ctx())
    assert len(query_outbox("run-1")) == 1
