"""Tests for ``GET /v1/runs/{run_id}/events`` — DDB-backed polling endpoint."""

from __future__ import annotations

import json
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
    """Spin up moto-backed DDB and force the dev-mode auth short-circuit."""
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS)
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


def seed_state(run_id: str, *, current_state: str) -> None:
    ddb().put_item(
        TableName=RUNS,
        Item={
            "pk": {"S": f"RUN#{run_id}"},
            "sk": {"S": "STATE"},
            "current_state": {"S": current_state},
            "status": {"S": "RUNNING"},
            "project_slug": {"S": "acme"},
        },
    )


def seed_event(run_id: str, *, sk: str, event_type: str, timestamp: str) -> None:
    envelope = json.dumps(
        {
            "event_id": f"evt-{sk}",
            "type": event_type,
            "timestamp": timestamp,
            "payload": {"k": "v"},
        }
    )
    ddb().put_item(
        TableName=RUNS,
        Item={
            "pk": {"S": f"RUN#{run_id}"},
            "sk": {"S": sk},
            "type": {"S": event_type},
            "envelope": {"S": envelope},
        },
    )


def test_returns_all_events_without_cursor() -> None:
    seed_state("r1", current_state="received")
    seed_event("r1", sk="EVENT#0001", event_type="REQUEST.RECEIVED", timestamp="t1")
    seed_event("r1", sk="EVENT#0002", event_type="ISSUE.TRIAGED", timestamp="t2")

    with TestClient(app) as client:
        resp = client.get("/v1/runs/r1/events")

    assert resp.status_code == 200
    body = resp.json()
    assert [e["type"] for e in body["events"]] == ["REQUEST.RECEIVED", "ISSUE.TRIAGED"]
    assert body["terminal"] is False


def test_filters_events_after_cursor() -> None:
    seed_state("r2", current_state="received")
    seed_event("r2", sk="EVENT#0001", event_type="REQUEST.RECEIVED", timestamp="t1")
    seed_event("r2", sk="EVENT#0002", event_type="ISSUE.TRIAGED", timestamp="t2")

    with TestClient(app) as client:
        resp = client.get("/v1/runs/r2/events", params={"since": "EVENT#0001"})

    assert resp.status_code == 200
    body = resp.json()
    assert [e["type"] for e in body["events"]] == ["ISSUE.TRIAGED"]


def test_signals_terminal_for_done_runs() -> None:
    seed_state("r3", current_state="done")
    seed_event("r3", sk="EVENT#0001", event_type="RUN.COMPLETED", timestamp="t1")

    with TestClient(app) as client:
        resp = client.get("/v1/runs/r3/events")

    assert resp.status_code == 200
    body = resp.json()
    assert body["terminal"] is True
