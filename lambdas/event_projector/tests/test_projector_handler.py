"""Projector handler tests — event row insert + SUMMARY row accumulators."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import moto
import pytest
from event_projector.handler import handler as project_event

from event_projector import handler as projector

TABLE = "runs"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub AWS env so the projector + moto use the same identifiers."""
    monkeypatch.setenv("AIDLC_RUNS_TABLE", TABLE)
    monkeypatch.setenv("AIDLC_MEMORY_ID", "memory-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


@pytest.fixture
def runs_table(aws_env: None) -> Any:
    """Create the runs table in moto and yield the DDB client."""
    del aws_env
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE,
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
        # The projector caches the boto3 client at import; clear so the
        # moto-backed client is what it sees.
        projector.ddb.cache_clear()
        yield client


def eb_event(envelope: dict[str, Any]) -> dict[str, Any]:
    """Wrap an envelope as it appears in an EventBridge → Lambda invocation."""
    return {
        "version": "0",
        "id": "test-id",
        "detail-type": envelope["type"],
        "source": "aidlc",
        "account": "000000000000",
        "time": envelope.get("timestamp", "2026-05-14T00:00:00Z"),
        "region": "us-east-1",
        "resources": [],
        "detail": envelope,
    }


def envelope(
    *,
    event_type: str,
    event_id: str,
    payload: dict[str, Any],
    run_id: str = "run-1",
    timestamp: str = "2026-05-14T00:00:00Z",
) -> dict[str, Any]:
    """Build a minimum-viable envelope dict."""
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "type": event_type,
        "timestamp": timestamp,
        "run_id": run_id,
        "correlation_id": "corr-1",
        "actor_id": "test",
        "payload": payload,
    }


def ctx() -> Any:
    """Stand-in for the Lambda context object used by Powertools."""
    return type(
        "Ctx",
        (),
        {
            "function_name": "test",
            "function_version": "$LATEST",
            "invoked_function_arn": "arn:aws:lambda:us-east-1:000000000000:function:test",
            "memory_limit_in_mb": 512,
            "aws_request_id": "req-1",
            "log_group_name": "/aws/lambda/test",
            "log_stream_name": "stream",
        },
    )()


def state_of(run_id: str, ddb: Any) -> dict[str, Any]:
    """Read the SUMMARY row's attributes for ``run_id``."""
    response = ddb.get_item(
        TableName=TABLE,
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "SUMMARY"}},
    )
    return response.get("Item", {})


def event_rows(run_id: str, ddb: Any) -> list[dict[str, Any]]:
    """Read every EVENT row for ``run_id`` from DDB."""
    response = ddb.query(
        TableName=TABLE,
        KeyConditionExpression="pk = :p AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={
            ":p": {"S": f"RUN#{run_id}"},
            ":prefix": {"S": "EVENT#"},
        },
    )
    return response.get("Items", [])


def test_request_received_creates_event_row_and_summary(runs_table: Any) -> None:
    """A REQUEST.RECEIVED event lands as an EVENT row + SUMMARY row."""
    env = envelope(
        event_type="REQUEST.RECEIVED",
        event_id="evt-1",
        payload={
            "project_slug": "demo",
            "intent": "fix bug",
            "requestor": "alice",
            "target_repo": "alice/repo",
            "source_issue_url": "https://github.com/alice/repo/issues/1",
        },
    )
    result = project_event(eb_event(env), ctx())
    assert result["committed"] is True
    rows = event_rows("run-1", runs_table)
    assert len(rows) == 1
    assert rows[0]["type"]["S"] == "REQUEST.RECEIVED"
    summary = state_of("run-1", runs_table)
    assert summary["status"]["S"] == "REQUEST.RECEIVED"
    assert summary["project_slug"]["S"] == "demo"
    assert summary["source_issue_url"]["S"] == "https://github.com/alice/repo/issues/1"
    assert summary["gsi1pk"]["S"] == "ISSUE#https://github.com/alice/repo/issues/1"
    assert summary["target_repo"]["S"] == "alice/repo"


def test_design_ready_accumulates_usage_and_advances_status(runs_table: Any) -> None:
    """``DESIGN.READY`` updates the status and adds token/cost totals."""
    project_event(
        eb_event(
            envelope(
                event_type="REQUEST.RECEIVED",
                event_id="evt-1",
                payload={"project_slug": "demo", "intent": "x", "requestor": "alice"},
            ),
        ),
        ctx(),
    )
    project_event(
        eb_event(
            envelope(
                event_type="DESIGN.READY",
                event_id="evt-2",
                payload={
                    "project_slug": "demo",
                    "plan_s3_key": "runs/run-1/plan.md",
                    "summary": "ok",
                    "session_id": "s1",
                    "token_in": 100,
                    "token_out": 50,
                    "cost_usd": 0.25,
                    "duration_ms": 1000,
                },
            ),
        ),
        ctx(),
    )
    summary = state_of("run-1", runs_table)
    assert summary["status"]["S"] == "DESIGN.READY"
    assert summary["total_token_in"]["N"] == "100"
    assert summary["total_token_out"]["N"] == "50"
    assert float(summary["total_cost_usd"]["N"]) == 0.25
    assert summary["total_duration_ms"]["N"] == "1000"


def test_redelivery_is_no_op(runs_table: Any) -> None:
    """Re-delivering the same event_id rolls back the transaction."""
    env = envelope(
        event_type="DESIGN.READY",
        event_id="evt-dup",
        payload={
            "project_slug": "demo",
            "plan_s3_key": "k",
            "summary": "s",
            "session_id": "s",
            "token_in": 10,
            "token_out": 5,
        },
    )
    first = project_event(eb_event(env), ctx())
    second = project_event(eb_event(env), ctx())
    assert first["committed"] is True
    assert second["committed"] is False
    rows = event_rows("run-1", runs_table)
    assert len(rows) == 1
    summary = state_of("run-1", runs_table)
    # Usage totals must not double-count on re-delivery.
    assert summary["total_token_in"]["N"] == "10"
    assert summary["total_token_out"]["N"] == "5"


def test_impl_pr_opened_sets_pr_url_and_gsi(runs_table: Any) -> None:
    """IMPL_PR.OPENED populates pr_url + gsi_pr so webhooks can look up the run."""
    pr_url = "https://github.com/alice/repo/pull/42"
    project_event(
        eb_event(
            envelope(
                event_type="IMPL_PR.OPENED",
                event_id="evt-1",
                payload={
                    "project_slug": "demo",
                    "pr_url": pr_url,
                    "diff_summary": "",
                    "session_id": "s",
                },
            ),
        ),
        ctx(),
    )
    summary = state_of("run-1", runs_table)
    assert summary["pr_url"]["S"] == pr_url
    assert summary["gsi_pr"]["S"] == f"PR#{pr_url}"


def test_unknown_trigger_returns_error() -> None:
    """Bare invocations without an EventBridge envelope are logged and skipped."""
    result = project_event({"not": "eventbridge"}, ctx())
    assert result == {"ok": False, "error": "unknown trigger"}


def test_event_row_carries_run_id_and_project_slug_for_pipe(runs_table: Any) -> None:
    """The EVENT row exposes ``run_id`` + ``project_slug`` so the Pipe input template works."""
    project_event(
        eb_event(
            envelope(
                event_type="REQUEST.RECEIVED",
                event_id="evt-pipe",
                payload={
                    "project_slug": "demo",
                    "intent": "x",
                    "requestor": "alice",
                    "target_repo": "alice/repo",
                },
            ),
        ),
        ctx(),
    )
    rows = event_rows("run-1", runs_table)
    assert rows[0]["run_id"]["S"] == "run-1"
    assert rows[0]["project_slug"]["S"] == "demo"
    envelope_json = json.loads(rows[0]["envelope"]["S"])
    assert envelope_json["type"] == "REQUEST.RECEIVED"


def test_event_id_idempotency_under_concurrent_delivery(runs_table: Any) -> None:
    """Two concurrent invocations with the same event_id leave one row only."""
    env = envelope(
        event_type="DESIGN.READY",
        event_id="evt-race",
        payload={
            "project_slug": "demo",
            "plan_s3_key": "k",
            "summary": "s",
            "session_id": "s",
            "token_in": 1,
        },
    )
    first = project_event(eb_event(env), ctx())
    second = project_event(eb_event(env), ctx())
    assert first["committed"] is True
    assert second["committed"] is False
    assert len(event_rows("run-1", runs_table)) == 1


def test_runs_table_env_var_drives_writes(aws_env: None) -> None:
    """Smoke-test that the env-var-driven module config is honoured."""
    del aws_env
    assert os.environ["AIDLC_RUNS_TABLE"] == TABLE
