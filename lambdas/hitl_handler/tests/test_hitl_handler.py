"""Unit tests for hitl_handler — moto-backed DDB + Step Functions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from hitl_handler.handler import ddb, handler, sfn
from moto import mock_aws

TABLE = "ai-dlc-test-approvals"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in for powertools."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="hitl_handler-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture
def state_machine_arn() -> Iterator[str]:
    """Create a trivial state machine and start an execution that waits on tokens."""
    role_arn = "arn:aws:iam::000000000000:role/StepFunctionsRole"
    definition = {
        "StartAt": "Wait",
        "States": {
            "Wait": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
                "Parameters": {"FunctionName": "noop", "Payload": {}},
                "End": True,
            },
        },
    }
    resp = sfn().create_state_machine(
        name="hitl-test",
        definition=json.dumps(definition),
        roleArn=role_arn,
    )
    yield resp["stateMachineArn"]


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars + create the approvals table."""
    monkeypatch.setenv("AIDLC_APPROVALS_TABLE", TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    ddb.cache_clear()
    sfn.cache_clear()
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
    sfn.cache_clear()


def request_approval(**overrides: Any) -> dict[str, Any]:
    body = {
        "op": "REQUEST_APPROVAL",
        "task_token": "fake-token",
        "run_id": "run-1",
        "project_slug": "demo",
        "gate_ref": "spec",
        "pr_url": "https://github.com/x/y/pull/1",
        "summary": "Spec ready for review",
    }
    body.update(overrides)
    return handler(body, ctx())


def test_request_approval_persists_token() -> None:
    out = request_approval()
    assert out["ok"] is True
    item = ddb().get_item(
        TableName=TABLE,
        Key={"pk": {"S": "RUN#run-1"}, "sk": {"S": "GATE#spec"}},
    )["Item"]
    assert item["task_token"]["S"] == "fake-token"
    assert item["status"]["S"] == "PENDING"


def test_decide_with_no_pending_returns_error() -> None:
    out = handler(
        {
            "op": "DECIDE",
            "run_id": "missing-run",
            "gate_ref": "spec",
            "decision": "approve",
            "reviewer": "bob",
        },
        ctx(),
    )
    assert out["ok"] is False
    assert out["error"]["kind"] == "not_found"


def test_unknown_op_returns_error() -> None:
    out = handler({"op": "FROBNICATE"}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "unknown_op"


def test_validation_error_on_missing_field() -> None:
    out = handler({"op": "REQUEST_APPROVAL", "run_id": "x"}, ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "validation_error"


def test_invalid_event_shape() -> None:
    out = handler(cast("dict[str, Any]", []), ctx())
    assert out["ok"] is False
    assert out["error"]["kind"] == "invalid_event"
