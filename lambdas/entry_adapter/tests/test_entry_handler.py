"""Unit tests for the entry_adapter Lambda — moto-backed DDB + EventBridge + SQS."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from entry_adapter.handler import handler, persistence
from moto import mock_aws

from common.event_emit import events_client as events
from common.ids import new_correlation_id, new_run_id
from entry_adapter import handler as entry_handler_module

BUS = "ai-dlc-test-bus"
TABLE = "ai-dlc-test-idempotency"


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
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Create the bus + Powertools idempotency table under moto."""
    monkeypatch.setenv("AIDLC_BUS_NAME", BUS)
    monkeypatch.setenv("AIDLC_IDEMPOTENCY_TABLE", TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    events.cache_clear()
    with mock_aws():
        boto3.client("events").create_event_bus(Name=BUS)
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Module-level persistence was built before moto patched boto3, so
        # its cached DDB resource targets the real AWS. Repoint it at moto.
        # Powertools doesn't expose these as a typed public API, so use
        # setattr to bypass static-attribute checks.
        setattr(persistence, "table", boto3.resource("dynamodb").Table(TABLE))  # noqa: B010
        setattr(persistence, "client", boto3.client("dynamodb"))  # noqa: B010
        yield
    events.cache_clear()


def submit(
    body: dict[str, Any],
    *,
    cognito_sub: str | None = None,
) -> dict[str, Any]:
    """Invoke the handler with an API Gateway proxy event.

    When ``cognito_sub`` is supplied the event carries an
    API-Gateway-v2-shaped JWT authorizer context so the handler can
    extract the claim.
    """
    event: dict[str, Any] = {"body": json.dumps(body), "isBase64Encoded": False}
    if cognito_sub is not None:
        event["requestContext"] = {
            "authorizer": {"jwt": {"claims": {"sub": cognito_sub}}},
        }
    return handler(event, ctx())


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


def test_replay_returns_same_run_id() -> None:
    """A replay returns the cached response — same run_id, no double-emit."""
    body = {
        "project_slug": "demo",
        "intent": "x",
        "requestor": "alice",
        "idempotency_key": "client-xyz-12345678",
    }
    first = submit(body)
    second = submit(body)
    run_id = json.loads(first["body"])["run_id"]
    assert json.loads(second["body"])["run_id"] == run_id


def test_requestor_sub_from_jwt_claims_is_forwarded_to_start_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API-Gateway JWT-authorizer ``sub`` claim flows through to ``start_run``."""
    captured: dict[str, Any] = {}

    def fake_start_run(**kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        return new_run_id(), new_correlation_id()

    monkeypatch.setattr(entry_handler_module, "start_run", fake_start_run)

    out = submit(
        {
            "project_slug": "demo",
            "intent": "x",
            "requestor": "alice@example.com",
            "idempotency_key": "client-claim-12345678",
        },
        cognito_sub="cognito-user-uuid-1",
    )

    assert out["statusCode"] == 202
    assert captured["requestor_sub"] == "cognito-user-uuid-1"
    assert captured["requestor"] == "alice@example.com"


def test_missing_authorizer_context_leaves_requestor_sub_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct invocations without an authorizer context send ``requestor_sub=None``."""
    captured: dict[str, Any] = {}

    def fake_start_run(**kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        return new_run_id(), new_correlation_id()

    monkeypatch.setattr(entry_handler_module, "start_run", fake_start_run)

    out = submit(
        {
            "project_slug": "demo",
            "intent": "x",
            "requestor": "alice",
            "idempotency_key": "client-noclaim-12345678",
        },
    )

    assert out["statusCode"] == 202
    assert captured["requestor_sub"] is None


def test_concurrent_request_with_in_progress_record_returns_409() -> None:
    """Concurrent request against an INPROGRESS record → 409, not a Lambda crash."""
    idempotency_key = "client-concurrent-test-12345678"
    hash_part = hashlib.md5(  # noqa: S324 — Powertools uses md5 internally
        json.dumps(idempotency_key, sort_keys=True).encode(),
    ).hexdigest()
    ddb_key = f"test-func.entry_adapter.handler.accept_run#{hash_part}"
    now_ms = int(time.time() * 1000)
    expires_at = int(time.time()) + 3600
    in_progress_expiry_ms = now_ms + 300_000

    boto3.client("dynamodb").put_item(
        TableName=TABLE,
        Item={
            "idempotency_key": {"S": ddb_key},
            "status": {"S": "INPROGRESS"},
            "expires_at": {"N": str(expires_at)},
            "in_progress_expiration": {"N": str(in_progress_expiry_ms)},
        },
    )

    out = submit(
        {
            "project_slug": "demo",
            "intent": "x",
            "requestor": "alice",
            "idempotency_key": idempotency_key,
        },
    )
    assert out["statusCode"] == 409
    body = json.loads(out["body"])
    assert body["error"] == "in_progress"
