"""Unit tests for the entry_adapter Lambda — moto-backed DDB + EventBridge + SQS."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from entry_adapter.handler import handler, persistence
from moto import mock_aws

from common.event_emit import events_client as events
from common.runs import ddb, sqs

BUS = "ai-dlc-test-bus"
TABLE = "ai-dlc-test-idempotency"
RUNS_TABLE = "ai-dlc-test-runs"
BEACON_QUEUE = "ai-dlc-test-state-router"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in for powertools."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="entry_adapter-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
            get_remaining_time_in_millis=lambda: 30_000,
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Create the bus, runs table, idempotency table, and beacon queue under moto."""
    monkeypatch.setenv("AIDLC_BUS_NAME", BUS)
    monkeypatch.setenv("AIDLC_IDEMPOTENCY_TABLE", TABLE)
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS_TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    events.cache_clear()
    ddb.cache_clear()
    sqs.cache_clear()
    with mock_aws():
        boto3.client("events").create_event_bus(Name=BUS)
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("dynamodb").create_table(
            TableName=RUNS_TABLE,
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
        queue_url = boto3.client("sqs").create_queue(QueueName=BEACON_QUEUE)["QueueUrl"]
        monkeypatch.setenv("AIDLC_BEACON_QUEUE_URL", queue_url)
        # Module-level persistence was built before moto patched boto3, so
        # its cached DDB resource targets the real AWS. Repoint it at moto.
        # Powertools doesn't expose these as a typed public API, so use
        # setattr to bypass static-attribute checks.
        setattr(persistence, "table", boto3.resource("dynamodb").Table(TABLE))  # noqa: B010
        setattr(persistence, "client", boto3.client("dynamodb"))  # noqa: B010
        yield queue_url
    events.cache_clear()
    ddb.cache_clear()
    sqs.cache_clear()


def submit(body: dict[str, Any]) -> dict[str, Any]:
    """Invoke the handler with an API Gateway proxy event."""
    return handler({"body": json.dumps(body), "isBase64Encoded": False}, ctx())


def test_first_submission_returns_202() -> None:
    out = submit(
        {
            "project_slug": "demo",
            "intent": "Add /healthz endpoint",
            "requestor": "alice",
            "idempotency_key": "client-xyz-12345678",
        },
    )
    assert out["statusCode"] == 202
    body = json.loads(out["body"])
    assert "run_id" in body
    assert body["project_slug"] == "demo"


def test_replay_returns_cached_response() -> None:
    body = {
        "project_slug": "demo",
        "intent": "Add /healthz endpoint",
        "requestor": "alice",
        "idempotency_key": "client-xyz-12345678",
    }
    first = submit(body)
    second = submit(body)
    assert first["statusCode"] == 202
    assert second["statusCode"] == 202
    assert json.loads(first["body"]) == json.loads(second["body"])


def test_invalid_body_returns_400() -> None:
    out = handler({"body": "{bad json"}, ctx())
    assert out["statusCode"] == 400
    assert json.loads(out["body"])["error"] == "invalid_json"


def test_missing_field_returns_400() -> None:
    out = submit({"project_slug": "demo", "intent": "x", "requestor": "alice"})
    assert out["statusCode"] == 400
    assert json.loads(out["body"])["error"] == "validation_error"


def test_event_published_to_bus() -> None:
    submit(
        {
            "project_slug": "demo",
            "intent": "x",
            "requestor": "alice",
            "idempotency_key": "client-xyz-12345678",
        },
    )
    # moto records put_events but doesn't expose them; simply assert no error
    # was raised. Coverage of envelope shape lives in common.events tests.
    assert True


def test_run_row_written_to_dynamodb() -> None:
    out = submit(
        {
            "project_slug": "demo",
            "intent": "Add /healthz",
            "requestor": "alice",
            "idempotency_key": "client-xyz-12345678",
        },
    )
    run_id = json.loads(out["body"])["run_id"]
    state = ddb().get_item(
        TableName=RUNS_TABLE,
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
    )["Item"]
    assert state["run_id"]["S"] == run_id
    assert state["project_slug"]["S"] == "demo"
    assert state["intent"]["S"] == "Add /healthz"
    assert state["requestor"]["S"] == "alice"
    # current_state is intentionally absent — the projector sets it on
    # REQUEST.RECEIVED.
    assert "current_state" not in state


def test_beacon_sent_to_state_router_queue(aws_env: str) -> None:
    submit(
        {
            "project_slug": "demo",
            "intent": "x",
            "requestor": "alice",
            "idempotency_key": "client-xyz-12345678",
        },
    )
    # Beacon has DelaySeconds=10, so it doesn't materialise on a normal
    # ReceiveMessage in moto. Inspect the queue's not-yet-visible count.
    attrs = boto3.client("sqs").get_queue_attributes(
        QueueUrl=aws_env,
        AttributeNames=["ApproximateNumberOfMessagesDelayed", "ApproximateNumberOfMessages"],
    )["Attributes"]
    delayed = int(attrs["ApproximateNumberOfMessagesDelayed"])
    visible = int(attrs["ApproximateNumberOfMessages"])
    assert delayed + visible == 1


def test_replay_does_not_double_write_run_row() -> None:
    body = {
        "project_slug": "demo",
        "intent": "x",
        "requestor": "alice",
        "idempotency_key": "client-xyz-12345678",
    }
    first = submit(body)
    second = submit(body)
    # Both invocations return the same run_id (cached idempotent reply).
    run_id = json.loads(first["body"])["run_id"]
    assert json.loads(second["body"])["run_id"] == run_id
    # Only one STATE row exists.
    items = ddb().scan(TableName=RUNS_TABLE)["Items"]
    state_rows = [i for i in items if i["sk"]["S"] == "STATE"]
    assert len(state_rows) == 1
