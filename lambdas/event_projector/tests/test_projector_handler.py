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


def envelope(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1.0",
        "event_id": "01J0000000000000000000000A",
        "type": "SPEC.READY",
        "timestamp": "2026-05-01T12:00:00Z",
        "run_id": "run-1",
        "correlation_id": "cor-1",
        "actor_id": "system",
        "payload": {
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "spec_s3_prefix": "specs/add-healthz/",
            "requirements_summary": "x",
            "design_summary": "y",
            "task_count": 2,
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


def test_eventbridge_event_writes_run_row() -> None:
    out = handler(eb_event(envelope()), ctx())
    assert out["ok"] is True
    items = ddb().query(
        TableName=TABLE,
        KeyConditionExpression="pk = :p AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":p": {"S": "RUN#run-1"}, ":prefix": {"S": "EVENT#"}},
    )["Items"]
    assert len(items) == 1
    assert items[0]["type"]["S"] == "SPEC.READY"


def test_redelivered_event_completes_projection() -> None:
    """Redelivery of the same event_id is idempotent; later steps still run.

    Without the CCFE swallow in ``upsert_run_event``, a redelivery would
    short-circuit on the EVENT-row PutItem and skip the state transition
    + memory forward. EventBridge retries (e.g. after a transient
    downstream failure on a prior attempt) need to make forward progress.
    """
    received = envelope(
        type="REQUEST.RECEIVED",
        payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
    )
    handler(eb_event(received), ctx())
    # Simulate a redelivery — same envelope, same event_id.
    out = handler(eb_event(received), ctx())
    assert out["ok"] is True
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    # Single state transition despite two deliveries.
    assert state["current_state"]["S"] == "received"
    assert state["state_transitions"]["N"] == "1"


def test_run_state_row_upserted_with_status() -> None:
    handler(eb_event(envelope()), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["status"]["S"] == "SPEC.READY"
    assert state["project_slug"]["S"] == "demo"
    assert state["spec_slug"]["S"] == "add-healthz"
    # spec_s3_prefix is needed by every downstream dispatch (critic,
    # implementer, reviewer, tester, open_spec_pr); persisting it on
    # SPEC.READY keeps the router's payloads valid.
    assert state["spec_s3_prefix"]["S"] == "specs/add-healthz/"


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
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["current_state"]["S"] == "received"
    assert state["last_event_id"]["S"] == "01J0000000000000000000000A"
    assert state["state_transitions"]["N"] == "1"


def test_two_request_received_events_only_transition_once() -> None:
    """A second REQUEST.RECEIVED with a different event_id no-ops the state transition.

    The projector's per-event timeline row is uniquely keyed on event_id
    (raises on actual duplicates); this test exercises the state-machine
    side, where the second event arrives after current_state has been
    set so the conditional update fails and we silently no-op.
    """
    first = envelope(
        type="REQUEST.RECEIVED",
        event_id="01J0000000000000000000000A",
        payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
    )
    second = envelope(
        type="REQUEST.RECEIVED",
        event_id="01J0000000000000000000000B",  # different event_id
        payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
    )
    handler(eb_event(first), ctx())
    handler(eb_event(second), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["current_state"]["S"] == "received"
    assert state["state_transitions"]["N"] == "1"  # not 2


def test_spec_ready_advances_state_when_run_in_architect_running() -> None:
    """SPEC.READY arriving in architect_running advances to spec_drafted."""
    # Seed the state row at architect_running.
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
        },
    )
    handler(eb_event(envelope(type="SPEC.READY")), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["current_state"]["S"] == "spec_drafted"


def test_spec_ready_no_op_when_not_in_architect_running() -> None:
    """SPEC.READY arriving in the wrong state is silently ignored."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "received"},
        },
    )
    handler(eb_event(envelope(type="SPEC.READY")), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["current_state"]["S"] == "received"


def test_run_failed_advances_any_non_terminal_to_failed() -> None:
    """RUN.FAILED short-circuits any state into failed."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "tasks_in_progress"},
        },
    )
    failed = envelope(
        type="RUN.FAILED",
        payload={
            "project_slug": "demo",
            "failed_state": "tasks_in_progress",
            "error_class": "Timeout",
            "error_message": "agent stalled",
            "retryable": False,
        },
    )
    handler(eb_event(failed), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["current_state"]["S"] == "failed"


def test_task_ready_advances_task_status_to_pr_open() -> None:
    """TASK.READY moves a TASK row from implementer_running to pr_open."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "implementer_running"},
        },
    )
    task_ready = envelope(
        type="TASK.READY",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "diff_summary": "x",
            "session_id": "run-1-T-001",
        },
    )
    handler(eb_event(task_ready), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["status"]["S"] == "pr_open"


def test_task_blocked_advances_task_status_to_blocked() -> None:
    """TASK.BLOCKED moves a TASK row from implementer_running to blocked.

    Regression: ``TASK.BLOCKED`` must be in ``TASK_LEVEL_EVENTS`` or the
    projector routes it to the run-level transition path (where
    ``apply_run_transition`` returns ``None``) and the task row stays
    stuck in ``implementer_running``.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "implementer_running"},
        },
    )
    task_blocked = envelope(
        type="TASK.BLOCKED",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "blocked_reason": "agent produced no diff",
            "session_id": "run-1-T-001",
        },
    )
    handler(eb_event(task_blocked), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["status"]["S"] == "blocked"
    assert task["pr_url"]["S"] == "https://github.com/o/r/pull/1"


def test_task_iteration_requested_appends_feedback_and_delivery_id() -> None:
    """TASK.ITERATION_REQUESTED adds delivery_id + feedback alongside state advance."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "pr_open"},
        },
    )
    iteration = envelope(
        type="TASK.ITERATION_REQUESTED",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "delivery_id": "webhook-1",
            "feedback": {
                "kind": "ci_failure",
                "workflow_name": "ci",
                "conclusion": "failure",
                "head_sha": "abcdef0",
                "html_url": "https://github.com/o/r/actions/runs/1",
            },
        },
    )
    handler(eb_event(iteration), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["status"]["S"] == "iterating"
    assert task["delivery_ids"]["SS"] == ["webhook-1"]
    assert len(task["pending_feedback"]["L"]) == 1
    feedback_entry = task["pending_feedback"]["L"][0]["M"]
    assert feedback_entry["kind"]["S"] == "ci_failure"
    assert feedback_entry["workflow_name"]["S"] == "ci"


def test_task_ready_from_iterating_flushes_pending_feedback() -> None:
    """Iteration N's TASK.READY clears pending_feedback + delivery_ids.

    Otherwise iteration N+1 dispatches the implementer with stale items
    from iterations 1..N still on the queue.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "iterating"},
            "delivery_ids": {"SS": ["webhook-1"]},
            "pending_feedback": {
                "L": [{"M": {"kind": {"S": "ci_failure"}, "workflow_name": {"S": "ci"}}}],
            },
        },
    )
    task_ready = envelope(
        type="TASK.READY",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "diff_summary": "fix",
            "session_id": "run-1-T-001",
        },
    )
    handler(eb_event(task_ready), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["status"]["S"] == "pr_open"
    assert task["pending_feedback"]["L"] == []
    assert "delivery_ids" not in task


def test_task_iteration_requested_in_iterating_accumulates_without_advance() -> None:
    """Second /aidlc fix while implementer is mid-iteration queues feedback.

    Without this, the projector early-returns (no transition for
    iterating → iterating) and the second request is silently dropped.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "iterating"},
            "delivery_ids": {"SS": ["webhook-1"]},
            "pending_feedback": {
                "L": [{"M": {"kind": {"S": "ci_failure"}, "workflow_name": {"S": "ci"}}}],
            },
        },
    )
    second_iteration = envelope(
        type="TASK.ITERATION_REQUESTED",
        event_id="01J0000000000000000000000C",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "delivery_id": "webhook-2",
            "feedback": {
                "kind": "issue_comment_mention",
                "comment_id": 7,
                "body": "also fix the lint",
                "commenter": "alice",
            },
        },
    )
    handler(eb_event(second_iteration), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    # State unchanged — implementer is still working on the first request.
    assert task["status"]["S"] == "iterating"
    # New delivery + feedback queued alongside the first.
    assert sorted(task["delivery_ids"]["SS"]) == ["webhook-1", "webhook-2"]
    assert len(task["pending_feedback"]["L"]) == 2
    second = task["pending_feedback"]["L"][1]["M"]
    assert second["kind"]["S"] == "issue_comment_mention"
    assert second["body"]["S"] == "also fix the lint"


def test_task_iteration_requested_in_implementer_running_accumulates() -> None:
    """Iteration request mid-implementer queues feedback, no state change."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "implementer_running"},
        },
    )
    iteration = envelope(
        type="TASK.ITERATION_REQUESTED",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "delivery_id": "webhook-3",
            "feedback": {
                "kind": "ci_failure",
                "workflow_name": "ci",
                "conclusion": "failure",
                "head_sha": "abcdef0",
                "html_url": "https://github.com/o/r/actions/runs/2",
            },
        },
    )
    handler(eb_event(iteration), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["status"]["S"] == "implementer_running"
    assert task["delivery_ids"]["SS"] == ["webhook-3"]
    assert len(task["pending_feedback"]["L"]) == 1


def test_request_received_with_source_issue_url_indexes_state_row() -> None:
    received = envelope(
        type="REQUEST.RECEIVED",
        payload={
            "project_slug": "demo",
            "intent": "Add /version endpoint",
            "requestor": "alice",
            "source_issue_url": "https://github.com/o/r/issues/7",
        },
    )

    handler(eb_event(received), ctx())

    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["gsi1pk"]["S"] == "ISSUE#https://github.com/o/r/issues/7"
    assert state["gsi1sk"]["S"] == "RUN#run-1"
    assert state["source_issue_url"]["S"] == "https://github.com/o/r/issues/7"


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

    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert "gsi1pk" not in state
    assert "gsi1sk" not in state


def test_run_completed_sets_tasks_completed_only() -> None:
    """RUN.COMPLETED no longer carries totals — accumulation owns those."""
    completed = envelope(
        type="RUN.COMPLETED",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "tasks_completed": 3,
        },
    )
    handler(eb_event(completed), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["status"]["S"] == "RUN.COMPLETED"
    assert state["tasks_completed"]["N"] == "3"


def test_per_event_usage_accumulates_on_state_row() -> None:
    """Each *.READY event with token/cost fields ADDs to running totals."""
    spec_ready = envelope(
        type="SPEC.READY",
        event_id="01J0000000000000000000000B",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "token_in": 4_000,
            "token_out": 1_500,
            "cost_usd": 0.25,
            "duration_ms": 30_000,
            "task_count": 2,
            "session_id": "run-1",
        },
    )
    review_ready = envelope(
        type="REVIEW.READY",
        event_id="01J0000000000000000000000C",
        payload={
            "project_slug": "demo",
            "task_id": "T-001",
            "verdict": "approve",
            "token_in": 1_000,
            "token_out": 500,
            "cost_usd": 0.05,
            "duration_ms": 8_000,
            "session_id": "run-1-T-001-reviewer",
        },
    )

    handler(eb_event(spec_ready), ctx())
    handler(eb_event(review_ready), ctx())

    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["total_token_in"]["N"] == "5000"
    assert state["total_token_out"]["N"] == "2000"
    assert float(state["total_cost_usd"]["N"]) == pytest.approx(0.30)
    assert state["total_duration_ms"]["N"] == "38000"


def test_event_with_zero_usage_skips_add() -> None:
    """Zero usage skips the ADD clause to avoid a no-op DDB write."""
    spec_ready = envelope(
        type="SPEC.READY",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "token_in": 0,
            "token_out": 0,
            "cost_usd": 0.0,
            "duration_ms": 0,
            "task_count": 1,
            "session_id": "run-1",
        },
    )
    handler(eb_event(spec_ready), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert "total_token_in" not in state
    assert "total_cost_usd" not in state


def test_duplicate_event_id_silently_skipped() -> None:
    """Redelivery returns ok and doesn't double-write the EVENT row."""
    env = envelope()
    handler(eb_event(env), ctx())
    out = handler(eb_event(env), ctx())
    assert out["ok"] is True
    items = ddb().query(
        TableName=TABLE,
        KeyConditionExpression="pk = :p AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":p": {"S": "RUN#run-1"}, ":prefix": {"S": "EVENT#"}},
    )["Items"]
    assert len(items) == 1


def test_ddb_stream_event_passthrough() -> None:
    out = handler(
        {
            "Records": [
                {
                    "eventID": "1",
                    "eventName": "INSERT",
                    "eventSource": "aws:dynamodb",
                    "eventVersion": "1.1",
                    "awsRegion": "us-east-1",
                    "dynamodb": {"SequenceNumber": "1", "Keys": {"pk": {"S": "RUN#1"}}},
                },
            ],
        },
        ctx(),
    )
    assert out == {"batchItemFailures": []}


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
    """``CreateEvent`` requires eventTimestamp + tagged-union payload.

    AgentCore Memory rejects:
      * missing ``eventTimestamp``
      * a ``payload[]`` entry that sets fields outside the
        ``conversational`` / ``blob`` tagged union (no ``contentType``,
        and ``blob`` carries a Document, not raw bytes).
    """
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
    assert entry["blob"]["type"] == "SPEC.READY"
    assert entry["blob"]["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# Dispatch circuit-breaker counter reset
# ---------------------------------------------------------------------------


def test_spec_ready_resets_run_dispatch_failure_count() -> None:
    """A successful architect dispatch zeroes the run-row breaker counter.

    The state-router increments ``dispatch_failure_count`` atomically on
    each rollback. An eventual SPEC.READY proves the dispatch reached the
    agent and ran; the projector clears the counter so a future
    intermittent failure starts from zero rather than the accumulated
    history.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "STATE"},
            "current_state": {"S": "architect_running"},
            "dispatch_failure_count": {"N": "2"},
        },
    )
    handler(eb_event(envelope(type="SPEC.READY")), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["current_state"]["S"] == "spec_drafted"
    assert state["dispatch_failure_count"]["N"] == "0"


def test_critique_ready_resets_run_dispatch_failure_count() -> None:
    """CRITIQUE.READY closes the breaker on the run row.

    Same shape as the SPEC.READY case; CRITIQUE.READY is the critic
    agent's terminal event, equally proof of a successful dispatch.
    """
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
            "spec_slug": "add-healthz",
            "critique_s3_key": "runs/run-1/critique.md",
            "issue_count": 0,
            "summary": "no issues",
            "session_id": "run-1-critic",
        },
    )
    handler(eb_event(critique), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["dispatch_failure_count"]["N"] == "0"


def test_task_ready_resets_task_dispatch_failure_count() -> None:
    """TASK.READY zeroes the breaker counter on the task row."""
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "implementer_running"},
            "dispatch_failure_count": {"N": "2"},
        },
    )
    task_ready = envelope(
        type="TASK.READY",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "diff_summary": "fix",
            "session_id": "run-1-T-001",
        },
    )
    handler(eb_event(task_ready), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["dispatch_failure_count"]["N"] == "0"


def test_task_blocked_also_resets_task_dispatch_failure_count() -> None:
    """TASK.BLOCKED is also a successful dispatch (the agent ran).

    The agent decided it couldn't produce a diff and self-reported
    ``BLOCKED`` — that's a clean run-to-completion, not a dispatch
    failure. The counter should reset so the next iteration starts
    fresh.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "implementer_running"},
            "dispatch_failure_count": {"N": "1"},
        },
    )
    task_blocked = envelope(
        type="TASK.BLOCKED",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "blocked_reason": "agent produced no diff",
            "session_id": "run-1-T-001",
        },
    )
    handler(eb_event(task_blocked), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["dispatch_failure_count"]["N"] == "0"


def test_task_approved_does_not_touch_dispatch_failure_count() -> None:
    """Non-dispatch-completion events leave the counter alone.

    TASK.APPROVED is a human action (reviewer approved the PR), not a
    proof that the most recent dispatch worked. The counter only resets
    on the events listed in ``DISPATCH_RESET_EVENTS``.
    """
    ddb().put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "RUN#run-1"},
            "sk": {"S": "TASK#T-001"},
            "status": {"S": "pending_approval"},
            "dispatch_failure_count": {"N": "2"},
        },
    )
    task_approved = envelope(
        type="TASK.APPROVED",
        payload={
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/o/r/pull/1",
            "reviewer": "alice",
        },
    )
    handler(eb_event(task_approved), ctx())
    task = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "TASK#T-001"}},
    )["Item"]
    assert task["status"]["S"] == "merged"
    assert task["dispatch_failure_count"]["N"] == "2"  # unchanged
