"""Tests for the dashboard's GitHub webhook handler."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from dashboard.routes.webhooks import (
    decision_from_comment,
    decision_from_review,
    parse_decision,
    parse_run_meta,
    verify_signature,
    webhook_secret,
)

SECRET = b"super-secret"


@pytest.fixture(autouse=True)
def patch_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the cached secret without going to Secrets Manager."""
    webhook_secret.cache_clear()

    fake_settings = MagicMock()
    fake_settings.github_webhook_secret_id = "/aidlc/dev/github-webhook-secret"  # noqa: S105
    monkeypatch.setattr("dashboard.routes.webhooks.settings", lambda: fake_settings)

    def fake_secrets() -> Any:
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": SECRET.decode("utf-8")}
        return client

    monkeypatch.setattr("dashboard.routes.webhooks.secrets", fake_secrets)
    yield
    webhook_secret.cache_clear()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid() -> None:
    body = b'{"foo":1}'
    verify_signature(body=body, signature_header=sign(body))


def test_verify_signature_rejects_missing_header() -> None:
    with pytest.raises(HTTPException):
        verify_signature(body=b"x", signature_header=None)


def test_verify_signature_rejects_wrong_signature() -> None:
    with pytest.raises(HTTPException):
        verify_signature(body=b"x", signature_header="sha256=deadbeef")


def test_parse_run_meta_extracts_fields() -> None:
    body = "_run_id: abc123_  ·  _correlation_id: xyz_\n\ngate_ref: spec"
    run_id, gate_ref = parse_run_meta(body)
    assert run_id == "abc123"
    assert gate_ref == "spec"


def test_parse_run_meta_returns_none_when_missing() -> None:
    assert parse_run_meta("just a regular PR body") == (None, None)


def test_decision_from_review_approved() -> None:
    payload = {
        "review": {"state": "approved", "user": {"login": "alice"}},
        "pull_request": {"body": "_run_id: r1_\ngate_ref: spec"},
    }
    decision = decision_from_review(payload)
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["reviewer"] == "alice"


def test_decision_from_review_changes_requested_maps_to_reject() -> None:
    payload = {
        "review": {"state": "changes_requested", "user": {"login": "alice"}, "body": "nope"},
        "pull_request": {"body": "_run_id: r1_\ngate_ref: spec"},
    }
    decision = decision_from_review(payload)
    assert decision is not None
    assert decision["decision"] == "reject"


def test_decision_from_review_ignores_dismissed() -> None:
    payload = {
        "review": {"state": "dismissed", "user": {"login": "alice"}},
        "pull_request": {"body": "_run_id: r1_\ngate_ref: spec"},
    }
    assert decision_from_review(payload) is None


def test_decision_from_comment_approve() -> None:
    payload = {
        "action": "created",
        "comment": {"body": "lgtm! /aidlc approve", "user": {"login": "alice"}},
        "issue": {"pull_request": {}, "body": "_run_id: r1_\ngate_ref: task:T-001"},
    }
    decision = decision_from_comment(payload)
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["gate_ref"] == "task:T-001"


def test_decision_from_comment_reject_with_reason() -> None:
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc reject please rethink the design", "user": {"login": "bob"}},
        "issue": {"pull_request": {}, "body": "_run_id: r1_\ngate_ref: spec"},
    }
    decision = decision_from_comment(payload)
    assert decision is not None
    assert decision["decision"] == "reject"
    assert decision["reason"] == "please rethink the design"


def test_decision_from_comment_ignores_non_pr() -> None:
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc approve", "user": {"login": "alice"}},
        "issue": {"body": "_run_id: r1_\ngate_ref: spec"},  # no pull_request key
    }
    assert decision_from_comment(payload) is None


def test_parse_decision_routes_correctly() -> None:
    review_payload = {
        "review": {"state": "approved", "user": {"login": "alice"}},
        "pull_request": {"body": "_run_id: r1_\ngate_ref: spec"},
    }
    assert parse_decision("pull_request_review", review_payload) is not None
    assert parse_decision("ping", {}) is None


def test_decision_round_trips_through_json() -> None:
    """Decision payload must be JSON-serialisable for Lambda invoke."""
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc approve", "user": {"login": "alice"}},
        "issue": {"pull_request": {}, "body": "_run_id: r1_\ngate_ref: spec"},
    }
    decision = decision_from_comment(payload)
    assert decision is not None
    assert json.dumps(decision)  # smoke test
