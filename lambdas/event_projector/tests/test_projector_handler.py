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
        KeyConditionExpression="pk = :p",
        ExpressionAttributeValues={":p": {"S": "RUN#run-1"}},
    )["Items"]
    assert len(items) == 1
    assert items[0]["type"]["S"] == "SPEC.READY"


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
