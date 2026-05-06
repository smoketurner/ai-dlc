"""Unit tests for event_projector — moto-backed runs table; agentcore is mocked."""

from __future__ import annotations

import json
from collections.abc import Iterator
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
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars, mock agentcore client, create the runs table under moto."""
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
        yield
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
        "source": "ai-dlc.system",
        "detail-type": env["type"],
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


def test_run_state_row_upserted_with_status() -> None:
    handler(eb_event(envelope()), ctx())
    state = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["status"]["S"] == "SPEC.READY"
    assert state["project_slug"]["S"] == "demo"
    assert state["spec_slug"]["S"] == "add-healthz"


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
    env = envelope()
    handler(eb_event(env), ctx())
    # second invocation with same event_id+timestamp triggers the
    # ConditionalCheckFailedException; the projector swallows it.
    with pytest.raises(Exception, match="ConditionalCheckFailed"):
        handler(eb_event(env), ctx())


def test_ddb_stream_event_passthrough() -> None:
    out = handler({"Records": [{"eventName": "INSERT"}]}, ctx())
    assert out == {"ok": True, "records": 1}


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
