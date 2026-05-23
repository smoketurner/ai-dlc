"""Tests for POST /v1/runs — idempotency under transient ``start_run`` failure.

Covers the regression in https://github.com/smoketurner/ai-dlc/issues/79:
the old custom idempotency table reservation wrote a final ``run_id``
before ``start_run`` succeeded, so a transient publish failure left a
"poisoned" record that returned a phantom ``run_id`` on retry. The fix
moves submission under Powertools' ``@idempotent_function`` decorator,
which manages an IN_PROGRESS → COMPLETED state machine and clears the
record on exception.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from common.ids import CorrelationId, RunId, new_correlation_id, new_run_id
from dashboard.app import app
from dashboard.deps import ddb, settings
from dashboard.routes import runs as runs_module

# Match conftest's module-level setenv so the persistence layer's cached
# ``table_name`` (set at import time, before any test fixture runs) lines up
# with the moto-created table we point it at below.
RUNS_TABLE = "ai-dlc-test-runs"
IDEMPOTENCY_TABLE = "ai-dlc-test-idempotency"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Spin up moto-backed DDB tables and stub start_run + EventBridge."""
    monkeypatch.setenv("AIDLC_ENV", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_BUS_NAME", "test-bus")
    monkeypatch.setenv("AIDLC_RUNS_TABLE", RUNS_TABLE)
    monkeypatch.setenv("AIDLC_IDEMPOTENCY_TABLE", IDEMPOTENCY_TABLE)
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
            TableName=RUNS_TABLE,
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
        client.create_table(
            TableName=IDEMPOTENCY_TABLE,
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "idempotency_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # Module-level persistence was built before moto patched boto3, so its
        # cached DDB resource targets real AWS. Repoint at moto (same trick the
        # entry_adapter tests use).
        persistence = runs_module.idempotency_persistence
        setattr(  # noqa: B010
            persistence, "table", boto3.resource("dynamodb").Table(IDEMPOTENCY_TABLE)
        )
        setattr(persistence, "client", client)  # noqa: B010
        yield
    settings.cache_clear()
    ddb.cache_clear()


def fake_start_run_factory(
    captured: list[dict[str, Any]],
    *,
    fail_first: bool = False,
) -> Any:
    """Build a stub ``start_run`` that records calls and mints fresh ids."""
    state = {"calls": 0}

    def fake_start_run(
        *,
        project_slug: str,
        intent: str,
        requestor: str,
        requestor_sub: str | None = None,
        target_repo: str | None = None,
        **_: Any,
    ) -> tuple[RunId, CorrelationId]:
        state["calls"] += 1
        captured.append(
            {
                "project_slug": project_slug,
                "intent": intent,
                "requestor": requestor,
                "requestor_sub": requestor_sub,
                "target_repo": target_repo,
            },
        )
        if fail_first and state["calls"] == 1:
            raise RuntimeError("EventBridge transient failure")
        return new_run_id(), new_correlation_id()

    return fake_start_run


def submit_payload(idempotency_key: str | None = "client-abc-12345678") -> dict[str, Any]:
    """Build a POST body for /v1/runs."""
    body: dict[str, Any] = {
        "intent": "add /healthz endpoint",
        "requestor": "alice",
        "target_repo": "acme/widgets",
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return body


def test_first_submission_returns_202(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path — first submission mints a run and returns 202."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runs_module, "start_run", fake_start_run_factory(calls))
    with TestClient(app) as client:
        resp = client.post("/v1/runs", json=submit_payload())
    assert resp.status_code == 202
    body = resp.json()
    assert body["run_id"]
    assert body["correlation_id"]
    assert body["project_slug"] == "acme-widgets"
    assert len(calls) == 1


def test_replay_returns_cached_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay with the same key — same cached payload, no double-emit."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runs_module, "start_run", fake_start_run_factory(calls))
    payload = submit_payload("client-replay-12345678")
    with TestClient(app) as client:
        first = client.post("/v1/runs", json=payload)
        second = client.post("/v1/runs", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert len(calls) == 1


def test_transient_failure_does_not_poison_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #79: a failed start_run must not leave a phantom run_id.

    First call: ``start_run`` raises → handler returns 500 (no record left
    behind). Second call with the same key: ``start_run`` succeeds and
    returns a brand-new ``run_id`` — never the orphaned id from attempt 1.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runs_module,
        "start_run",
        fake_start_run_factory(calls, fail_first=True),
    )
    payload = submit_payload("client-retry-12345678")
    # ``raise_server_exceptions=False`` so the first call surfaces as a 500
    # response (matching production ASGI behaviour) instead of bubbling the
    # RuntimeError out of the TestClient.
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post("/v1/runs", json=payload)
        assert first.status_code == 500
        second = client.post("/v1/runs", json=payload)
    assert second.status_code == 202
    body = second.json()
    assert body["run_id"]
    assert body["project_slug"] == "acme-widgets"
    assert len(calls) == 2


def test_in_progress_record_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent request against an IN_PROGRESS record gets a 409, not a crash."""
    monkeypatch.setattr(runs_module, "start_run", fake_start_run_factory([]))
    idempotency_key = "client-concurrent-12345678"
    hash_part = hashlib.md5(  # noqa: S324 — Powertools uses md5 internally
        json.dumps(idempotency_key, sort_keys=True).encode(),
    ).hexdigest()
    # Powertools keys are ``<function_qualified_name>#<md5>``; the qualified
    # name is the module + decorated function. ``accept_run`` is decorated at
    # module load, so its qualified name is stable.
    ddb_key = f"test-func.dashboard.routes.runs.accept_run#{hash_part}"
    now_ms = int(time.time() * 1000)
    boto3.client("dynamodb").put_item(
        TableName=IDEMPOTENCY_TABLE,
        Item={
            "idempotency_key": {"S": ddb_key},
            "status": {"S": "INPROGRESS"},
            "expires_at": {"N": str(int(time.time()) + 3600)},
            "in_progress_expiration": {"N": str(now_ms + 300_000)},
        },
    )
    with TestClient(app) as client:
        resp = client.post("/v1/runs", json=submit_payload(idempotency_key))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "in_progress"
