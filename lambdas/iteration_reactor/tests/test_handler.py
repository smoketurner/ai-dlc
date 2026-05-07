"""Unit tests for the iteration_reactor Lambda.

DDB is mocked via ``moto``; AgentCore Runtime + EventBridge are mocked
via monkeypatched module-level functions so tests stay sync and don't
spin up botocore stubs.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws

from common.events import EventEnvelope
from common.runtime import (
    CiFailureFeedback,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)
from iteration_reactor import handler as h


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="iteration_reactor-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000:function:t",
            aws_request_id="req-1",
        ),
    )


def webhook_event(
    *,
    trigger_kind: str = "ci_failure",
    trigger_payload: dict[str, Any] | None = None,
    delivery_id: str | None = None,
    pr_url: str = "https://github.com/owner/repo/pull/42",
    head_sha: str = "abcdef0",
) -> dict[str, Any]:
    """Build a minimum-valid webhook event for the reactor."""
    return {
        "trigger_kind": trigger_kind,
        "run_id": str(uuid4()),
        "task_id": "T-001",
        "correlation_id": str(uuid4()),
        "project_slug": "demo",
        "spec_slug": "add-healthz",
        "spec_s3_prefix": "specs/add-healthz/",
        "target_repo": "owner/repo",
        "pr_url": pr_url,
        "pr_number": 42,
        "head_sha": head_sha,
        "delivery_id": delivery_id or str(uuid4()),
        "trigger_payload": trigger_payload
        or {
            "workflow_name": "CI / test",
            "conclusion": "failure",
            "html_url": "https://github.com/owner/repo/actions/runs/1",
        },
    }


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standard env vars the reactor reads."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_RUNS_TABLE", "ai-dlc-test-runs")
    monkeypatch.setenv("AIDLC_BUS_NAME", "ai-dlc-test-bus")
    monkeypatch.setenv(
        "AIDLC_IMPLEMENTER_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:000:runtime/impl",
    )
    monkeypatch.setenv(
        "AIDLC_REVIEWER_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:000:runtime/rev",
    )
    monkeypatch.setenv(
        "AIDLC_TESTER_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:000:runtime/test",
    )
    # Force the cached clients to rebuild against the moto/mocked endpoints.
    h.ddb_client.cache_clear()
    h.runtime_client.cache_clear()


@pytest.fixture
def ddb_table() -> Iterable[None]:
    """Stand up the runs table in moto's DDB so reactor reads/writes work."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="ai-dlc-test-runs",
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
        h.ddb_client.cache_clear()
        yield


@pytest.fixture
def stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``fire_and_forget_invoke`` with a recorder."""
    calls: list[dict[str, Any]] = []

    def record(*, runtime_arn: str, runtime_session_id: str, payload: dict[str, Any]) -> bool:
        calls.append(
            {
                "runtime_arn": runtime_arn,
                "runtime_session_id": runtime_session_id,
                "payload": payload,
            },
        )
        return True

    monkeypatch.setattr(h, "fire_and_forget_invoke", record)
    return calls


@pytest.fixture
def stub_publish(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Replace ``common.event_emit.publish`` with a recorder."""
    captured: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(h, "publish", captured.append)
    return captured


# ---------------------------------------------------------------------------
# Input dispatch
# ---------------------------------------------------------------------------


def test_unknown_event_shape_returns_error() -> None:
    out = h.handler({"foo": "bar"}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_event_shape"


def test_validation_error_on_bad_webhook_input() -> None:
    bad = webhook_event()
    bad["trigger_kind"] = "not_a_real_kind"
    out = h.handler(bad, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


# ---------------------------------------------------------------------------
# Webhook trigger path
# ---------------------------------------------------------------------------


def test_first_iteration_dispatches_implementer(
    ddb_table: None,
    stub_dispatch: list[dict[str, Any]],
    stub_publish: list[EventEnvelope[Any]],
) -> None:
    out = h.handler(webhook_event(), ctx())
    assert out["ok"] is True
    assert out["dispatched"] == "implementer"
    assert out["iteration_count"] == 1
    # One implementer dispatch.
    assert len(stub_dispatch) == 1
    impl = stub_dispatch[0]
    assert impl["runtime_arn"].endswith("/impl")
    assert impl["payload"]["iteration_count"] == 1
    assert impl["payload"]["iteration_feedback"][0]["kind"] == "ci_failure"
    # Emits TASK.ITERATION_STARTED.
    started = [e for e in stub_publish if e.type == "TASK.ITERATION_STARTED"]
    assert len(started) == 1


def test_duplicate_delivery_id_skipped(
    ddb_table: None,
    stub_dispatch: list[dict[str, Any]],
    stub_publish: list[EventEnvelope[Any]],
) -> None:
    event = webhook_event(delivery_id="dup-1")
    h.handler(event, ctx())
    assert len(stub_dispatch) == 1
    # Same event again — should skip.
    out2 = h.handler(event, ctx())
    assert out2["ok"] is True
    assert out2["skipped"] == "duplicate_delivery"
    assert len(stub_dispatch) == 1  # no new dispatch


def test_max_iterations_short_circuits(
    ddb_table: None,
    stub_dispatch: list[dict[str, Any]],
    stub_publish: list[EventEnvelope[Any]],
) -> None:
    run_id = str(uuid4())
    for i in range(3):
        event = webhook_event(delivery_id=f"d-{i}")
        event["run_id"] = run_id
        h.handler(event, ctx())
    assert len(stub_dispatch) == 3
    # 4th should hit the budget cap.
    event4 = webhook_event(delivery_id="d-3")
    event4["run_id"] = run_id
    out = h.handler(event4, ctx())
    assert out["ok"] is True
    assert out["skipped"] == "max_iterations"
    assert len(stub_dispatch) == 3
    maxes = [e for e in stub_publish if e.type == "TASK.MAX_ITERATIONS_REACHED"]
    assert len(maxes) == 1


def test_post_iteration_comment_invokes_repo_helper(
    ddb_table: None,
    stub_dispatch: list[dict[str, Any]],
    stub_publish: list[EventEnvelope[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reactor drops a 'starting iteration N' comment on the PR via repo_helper."""
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", "test-repo-helper")
    h.repo_helper_function_name.cache_clear() if hasattr(
        h.repo_helper_function_name, "cache_clear"
    ) else None
    captured: list[dict[str, Any]] = []

    def fake_invoke(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"StatusCode": 200, "Payload": SimpleNamespace(read=lambda: b'{"ok": true}')}

    fake_client = SimpleNamespace(invoke=fake_invoke)
    monkeypatch.setattr(h, "lambda_client", lambda: fake_client)

    h.handler(webhook_event(), ctx())

    assert len(captured) == 1
    assert captured[0]["FunctionName"] == "test-repo-helper"
    body = captured[0]["Payload"]
    assert b'"op": "comment_pr"' in body
    assert b'"pr_number": 42' in body
    assert b"iteration" in body.lower()


def test_post_iteration_comment_is_skipped_when_function_unset(
    ddb_table: None,
    stub_dispatch: list[dict[str, Any]],
    stub_publish: list[EventEnvelope[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit tests don't set AIDLC_REPO_HELPER_FUNCTION_NAME → comment is skipped, not raised."""
    monkeypatch.delenv("AIDLC_REPO_HELPER_FUNCTION_NAME", raising=False)
    h.repo_helper_function_name.cache_clear() if hasattr(
        h.repo_helper_function_name, "cache_clear"
    ) else None
    out = h.handler(webhook_event(), ctx())
    assert out["ok"] is True
    assert out["dispatched"] == "implementer"


# ---------------------------------------------------------------------------
# build_feedback_item per kind
# ---------------------------------------------------------------------------


def test_build_feedback_item_ci_failure() -> None:
    trigger = h.WebhookTrigger.model_validate(webhook_event(trigger_kind="ci_failure"))
    item = h.build_feedback_item(trigger)
    assert isinstance(item, CiFailureFeedback)
    assert item.workflow_name == "CI / test"


def test_build_feedback_item_review_changes_requested() -> None:
    trigger = h.WebhookTrigger.model_validate(
        webhook_event(
            trigger_kind="review_changes_requested",
            trigger_payload={"reviewer": "alice", "body": "Fix the null", "review_id": 99},
        ),
    )
    item = h.build_feedback_item(trigger)
    assert isinstance(item, ReviewChangesRequestedFeedback)
    assert item.reviewer == "alice"
    assert item.review_id == 99


def test_build_feedback_item_review_comment_mention() -> None:
    trigger = h.WebhookTrigger.model_validate(
        webhook_event(
            trigger_kind="review_comment_mention",
            trigger_payload={
                "path": "src/x.py",
                "line": 42,
                "comment_id": 7,
                "body": "@aidlc-bot fix",
                "commenter": "alice",
            },
        ),
    )
    item = h.build_feedback_item(trigger)
    assert isinstance(item, ReviewCommentMentionFeedback)
    assert item.path == "src/x.py"
    assert item.line == 42


def test_build_feedback_item_issue_comment_mention() -> None:
    trigger = h.WebhookTrigger.model_validate(
        webhook_event(
            trigger_kind="issue_comment_mention",
            trigger_payload={"comment_id": 12, "body": "@aidlc-bot help", "commenter": "bob"},
        ),
    )
    item = h.build_feedback_item(trigger)
    assert isinstance(item, IssueCommentMentionFeedback)
    assert item.commenter == "bob"


# ---------------------------------------------------------------------------
# EventBridge path (TASK.ITERATION_COMMITTED → reviewer + tester)
# ---------------------------------------------------------------------------


def test_iteration_committed_dispatches_reviewer_and_tester(
    stub_dispatch: list[dict[str, Any]],
    stub_publish: list[EventEnvelope[Any]],
) -> None:
    run_id = str(uuid4())
    correlation_id = str(uuid4())
    event_id = str(uuid4())
    eb_event = {
        "detail-type": "TASK.ITERATION_COMMITTED",
        "source": "ai-dlc.implementer",
        "detail": {
            "schema_version": "1.0",
            "event_id": event_id,
            "type": "TASK.ITERATION_COMMITTED",
            "timestamp": "2026-05-06T12:00:00Z",
            "run_id": run_id,
            "correlation_id": correlation_id,
            "actor_id": "implementer",
            "payload": {
                "project_slug": "demo",
                "spec_slug": "add-healthz",
                "task_id": "T-001",
                "pr_url": "https://github.com/owner/repo/pull/42",
                "iteration_count": 2,
                "head_sha": "feedface00",
                "diff_summary": "Fix null-check.",
                "session_id": "sess",
            },
        },
    }
    out = h.handler(eb_event, ctx())
    assert out["ok"] is True
    assert out["dispatched"] == ["reviewer", "tester"]
    runtime_arns = sorted(d["runtime_arn"] for d in stub_dispatch)
    assert runtime_arns[0].endswith("/rev")
    assert runtime_arns[1].endswith("/test")


def test_iteration_committed_invalid_detail_returns_error() -> None:
    eb_event = {"detail-type": "TASK.ITERATION_COMMITTED", "detail": "not-a-dict"}
    out = h.handler(eb_event, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"
