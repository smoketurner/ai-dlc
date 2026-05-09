"""Tests for telemetry — moto-backed S3 + DDB; Bedrock client mocked."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws

from telemetry.handler import RejectionEnvelope, bedrock, build_record, handler, s3

ARTIFACTS = "test-artifacts"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in for powertools."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="telemetry-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


def fake_bedrock_client(label: str) -> Any:
    """Return a Bedrock client that always returns ``label`` from invoke_model."""
    body = json.dumps(
        {"content": [{"type": "text", "text": label}]},
    ).encode("utf-8")
    client = MagicMock()
    client.invoke_model.return_value = {"body": io.BytesIO(body)}
    return client


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars + create the artifacts bucket under moto."""
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    s3.cache_clear()
    bedrock.cache_clear()
    monkeypatch.setattr("telemetry.handler.bedrock", lambda: fake_bedrock_client("test-failed"))
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=ARTIFACTS)
        yield
    s3.cache_clear()
    bedrock.cache_clear()


def envelope(event_type: str = "TASK.REJECTED", **payload_overrides: Any) -> dict[str, Any]:
    """Build a rejection envelope."""
    base = {
        "schema_version": "1.0",
        "event_id": "01J0000000000000000000000A",
        "type": event_type,
        "timestamp": "2026-05-01T12:00:00Z",
        "run_id": "run-1",
        "correlation_id": "cor-1",
        "actor_id": "system",
        "payload": {
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "task_id": "T-001",
            "pr_url": "https://github.com/x/y/pull/1",
            "reviewer": "alice",
            "reason": "tests are failing on the new endpoint",
            **payload_overrides,
        },
    }
    return base


def eb(env: dict[str, Any]) -> dict[str, Any]:
    """Wrap the envelope in EventBridge shape (full schema for parse())."""
    return {
        "version": "0",
        "id": "11111111-2222-3333-4444-555555555555",
        "detail-type": env["type"],
        "source": "ai-dlc.system",
        "account": "000000000000",
        "time": "2026-05-01T12:00:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail": env,
    }


def test_categorizes_task_rejection_to_test_failed() -> None:
    out = handler(eb(envelope()), ctx())
    assert out["ok"] is True
    assert out["category"] == "test-failed"


def test_record_persisted_to_s3() -> None:
    handler(eb(envelope()), ctx())
    keys = [
        item["Key"]
        for item in s3()
        .list_objects_v2(Bucket=ARTIFACTS, Prefix="evals/rejections/")
        .get("Contents", [])
    ]
    assert any("/run-1/task_T-001.json" in k for k in keys)


def test_unknown_event_type_ignored() -> None:
    out = handler(eb(envelope(event_type="SPEC.READY")), ctx())
    assert out["ok"] is True
    assert out["ignored"] is True


def test_missing_detail_returns_error() -> None:
    out = handler({"source": "x"}, ctx())
    assert out["ok"] is False
    assert out["error"] == "validation_error"


def test_empty_reason_falls_back_to_other(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "telemetry.handler.bedrock", lambda: fake_bedrock_client("convention-violation")
    )
    out = handler(eb(envelope(reason="")), ctx())
    assert out["category"] == "other"


def test_unknown_label_falls_back_to_other(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "telemetry.handler.bedrock", lambda: fake_bedrock_client("not-a-real-label")
    )
    out = handler(eb(envelope()), ctx())
    assert out["category"] == "other"


def test_string_detail_payload_parsed() -> None:
    """EventBridge sometimes ships ``detail`` as a JSON string."""
    env = envelope()
    payload = eb(env)
    payload["detail"] = json.dumps(env)
    out = handler(payload, ctx())
    assert out["ok"] is True


def test_spec_rejection_uses_spec_gate_ref() -> None:
    out = handler(eb(envelope(event_type="SPEC.REJECTED")), ctx())
    assert out["gate_ref"] == "spec"


def test_build_record_carries_category_and_envelope() -> None:
    parsed = RejectionEnvelope.model_validate(envelope())
    record = build_record(envelope=parsed, gate_ref="spec", category="test-failed")
    assert record["category"] == "test-failed"
    assert record["envelope"]["run_id"] == "run-1"
