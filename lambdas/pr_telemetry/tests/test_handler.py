"""Tests for the PR telemetry Lambda — webhook dispatch + DDB writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws

from pr_telemetry.handler import (
    handler,
    parse_run_id,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

PR_URL = "https://github.com/owner/name/pull/42"
RUN_ID = "01956000-0000-7000-0000-000000000001"


def lambda_context() -> LambdaContext:
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="pr_telemetry-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture
def telemetry_table(monkeypatch: pytest.MonkeyPatch) -> DynamoDBClient:
    """Spin up a moto DDB table named in the AIDLC_PR_TELEMETRY_TABLE env."""
    name = "ai-dlc-pr-telemetry"
    monkeypatch.setenv("AIDLC_PR_TELEMETRY_TABLE", name)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=name,
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
        yield client


def opened_event(*, body: str = f"## work\n\n_run_id: {RUN_ID}_") -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "html_url": PR_URL,
            "body": body,
            "created_at": "2026-05-05T12:00:00Z",
            "draft": False,
        },
    }


def review_event(*, state: str) -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {"state": state},
        "pull_request": {"html_url": PR_URL},
    }


def comment_event(*, is_bot: bool) -> dict[str, Any]:
    return {
        "action": "created",
        "comment": {"user": {"type": "Bot" if is_bot else "User"}, "html_url": PR_URL},
        "issue": {"pull_request": {"html_url": PR_URL}},
    }


def closed_event(*, merged: bool) -> dict[str, Any]:
    return {
        "action": "closed",
        "pull_request": {
            "html_url": PR_URL,
            "body": f"_run_id: {RUN_ID}_",
            "closed_at": "2026-05-05T13:00:00Z",
            "merged_at": "2026-05-05T13:00:00Z" if merged else None,
            "merged": merged,
        },
    }


def ready_for_review_event() -> dict[str, Any]:
    return {
        "action": "ready_for_review",
        "pull_request": {
            "html_url": PR_URL,
            "body": f"_run_id: {RUN_ID}_",
            "updated_at": "2026-05-05T14:00:00Z",
            "user": {"login": "maintainer"},
        },
    }


def get_state(client: DynamoDBClient, table: str) -> dict[str, Any]:
    resp = client.get_item(TableName=table, Key={"pk": {"S": f"PR#{PR_URL}"}, "sk": {"S": "STATE"}})
    return resp.get("Item", {})


def test_parse_run_id_finds_marker() -> None:
    body = (
        "## summary\n\nDid the work.\n\n---\n"
        f"_run_id: {RUN_ID}_  ·  _correlation_id: x_"
    )
    assert parse_run_id(body) == RUN_ID


def test_parse_run_id_returns_none_when_missing() -> None:
    assert parse_run_id("just a regular PR body") is None


def test_handler_opened_writes_initial_row(telemetry_table: DynamoDBClient) -> None:
    table = "ai-dlc-pr-telemetry"
    out = handler(opened_event(), lambda_context())
    assert out["ok"]
    assert out["action"] == "opened"
    state = get_state(telemetry_table, table)
    assert state["run_id"]["S"] == RUN_ID
    assert state["opened_as_draft"]["BOOL"] is False
    assert state["requested_changes_count"]["N"] == "0"


def test_handler_ignores_third_party_pr(telemetry_table: DynamoDBClient) -> None:
    out = handler(opened_event(body="No marker."), lambda_context())
    assert out["ok"]
    assert out["ignored"] == "no_run_id_marker"
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert state == {}


def test_handler_review_changes_requested_increments_counts(
    telemetry_table: DynamoDBClient,
) -> None:
    handler(opened_event(), lambda_context())
    handler(review_event(state="changes_requested"), lambda_context())
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert state["requested_changes_count"]["N"] == "1"
    assert state["review_count"]["N"] == "1"


def test_handler_review_approved_only_increments_review_count(
    telemetry_table: DynamoDBClient,
) -> None:
    handler(opened_event(), lambda_context())
    handler(review_event(state="approved"), lambda_context())
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert state["requested_changes_count"]["N"] == "0"
    assert state["review_count"]["N"] == "1"


def test_handler_comment_human_vs_bot(telemetry_table: DynamoDBClient) -> None:
    handler(opened_event(), lambda_context())
    handler(comment_event(is_bot=False), lambda_context())
    handler(comment_event(is_bot=True), lambda_context())
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert state["comment_count_human"]["N"] == "1"
    assert state["comment_count_bot"]["N"] == "1"


def test_handler_closed_merged_sets_merged_fields(telemetry_table: DynamoDBClient) -> None:
    handler(opened_event(), lambda_context())
    out = handler(closed_event(merged=True), lambda_context())
    assert out["action"] == "closed"
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert state["merged"]["BOOL"] is True
    assert "merged_at" in state


def test_handler_closed_unmerged_clears_merged_flag(telemetry_table: DynamoDBClient) -> None:
    handler(opened_event(), lambda_context())
    handler(closed_event(merged=False), lambda_context())
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert state["merged"]["BOOL"] is False
    assert "merged_at" not in state


def test_handler_ready_for_review_records_marker(telemetry_table: DynamoDBClient) -> None:
    handler(opened_event(), lambda_context())
    handler(ready_for_review_event(), lambda_context())
    state = get_state(telemetry_table, "ai-dlc-pr-telemetry")
    assert "marked_ready_at" in state
    assert state["marked_ready_by"]["S"] == "maintainer"


def test_handler_unknown_shape_returns_error(telemetry_table: DynamoDBClient) -> None:
    out = handler({"foo": "bar"}, lambda_context())
    assert out["ok"] is False
