"""Tests for DELETE /v1/runs/{run_id} — terminal-only cascade delete."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from dashboard.app import app
from dashboard.deps import ddb, settings

RUNS = "test-runs"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Spin up moto-backed DDB tables and disable Cognito auth."""
    monkeypatch.setenv("AIDLC_ENV", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_BUS_NAME", "test-bus")
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS)
    monkeypatch.setenv("AIDLC_IDEMPOTENCY_TABLE", "test-idempotency")
    monkeypatch.setenv(
        "AIDLC_BEACON_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/000000000000/test-beacon",
    )
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", "test-artifacts")
    monkeypatch.setenv(
        "AIDLC_GITHUB_APP_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:0:secret:app"
    )
    monkeypatch.setenv("AIDLC_GITHUB_WEBHOOK_SECRET_ID", "test-secret")
    monkeypatch.setenv("AIDLC_COGNITO_USER_POOL_ID", "test-pool")
    monkeypatch.setenv("AIDLC_COGNITO_CLIENT_ID", "test-client")
    monkeypatch.setenv("AIDLC_AUTH", "disabled")
    settings.cache_clear()
    ddb.cache_clear()
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=RUNS,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    settings.cache_clear()
    ddb.cache_clear()


def seed_run(
    run_id: str,
    *,
    status: str,
    event_count: int = 0,
) -> None:
    """Seed a SUMMARY row plus ``event_count`` synthetic event rows for ``run_id``.

    ``status`` is the latest event type — the dashboard derives "is this
    run terminal?" from this field instead of a separate state cursor.
    """
    ddb().put_item(
        TableName=RUNS,
        Item={
            "pk": {"S": f"RUN#{run_id}"},
            "sk": {"S": "SUMMARY"},
            "status": {"S": status},
            "project_slug": {"S": "acme-widgets"},
        },
    )
    for i in range(event_count):
        ddb().put_item(
            TableName=RUNS,
            Item={
                "pk": {"S": f"RUN#{run_id}"},
                "sk": {"S": f"EVENT#{i:04d}"},
                "type": {"S": "REQUEST.RECEIVED"},
                "envelope": {"S": "{}"},
            },
        )


def count_partition(table: str, run_id: str) -> int:
    resp = ddb().query(
        TableName=table,
        KeyConditionExpression="pk = :p",
        ExpressionAttributeValues={":p": {"S": f"RUN#{run_id}"}},
    )
    return len(resp.get("Items", []))


def test_delete_run_returns_404_when_unknown() -> None:
    with TestClient(app) as client:
        resp = client.delete("/v1/runs/does-not-exist")
    assert resp.status_code == 404


def test_delete_run_returns_409_when_not_terminal() -> None:
    seed_run("r-active", status="REQUEST.RECEIVED", event_count=2)
    with TestClient(app) as client:
        resp = client.delete("/v1/runs/r-active")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "run_not_terminal"
    assert count_partition(RUNS, "r-active") == 3


def test_delete_run_cascades_when_done() -> None:
    seed_run("r-done", status="RUN.COMPLETED", event_count=30)
    with TestClient(app) as client:
        resp = client.delete("/v1/runs/r-done")
    assert resp.status_code == 204
    assert count_partition(RUNS, "r-done") == 0


def test_delete_run_handles_failed_state() -> None:
    seed_run("r-failed", status="RUN.FAILED", event_count=1)
    with TestClient(app) as client:
        resp = client.delete("/v1/runs/r-failed")
    assert resp.status_code == 204
    assert count_partition(RUNS, "r-failed") == 0


def test_delete_run_handles_cancelled_state() -> None:
    """Cancelled runs are terminal — used to be unreachable via the dashboard."""
    seed_run("r-cancelled", status="RUN.CANCEL_REQUESTED", event_count=1)
    with TestClient(app) as client:
        resp = client.delete("/v1/runs/r-cancelled")
    assert resp.status_code == 204
    assert count_partition(RUNS, "r-cancelled") == 0


def test_delete_run_does_not_touch_other_runs() -> None:
    seed_run("r-keep", status="RUN.COMPLETED", event_count=2)
    seed_run("r-drop", status="RUN.COMPLETED", event_count=2)
    with TestClient(app) as client:
        resp = client.delete("/v1/runs/r-drop")
    assert resp.status_code == 204
    assert count_partition(RUNS, "r-drop") == 0
    assert count_partition(RUNS, "r-keep") == 3
