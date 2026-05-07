"""Tests for the dashboard's GitHub webhook handler.

The handler translates GitHub events into platform events on the
EventBridge bus. Tests stub the ``publish`` helper to capture every
emitted envelope, and stub DDB lookups (``lookup_pr`` /
``lookup_run_by_issue``) to return canned rows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from common.events import EventEnvelope, RequestReceived
from common.ids import CorrelationId, RunId, new_correlation_id, new_event_id, new_run_id
from dashboard.routes.webhooks import (
    receive_github_webhook,
    verify_signature,
    webhook_secret,
)

SECRET = b"super-secret"
BOT_LOGIN = "aidlc-bot"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub settings + secrets so we never reach AWS."""
    webhook_secret.cache_clear()
    fake = MagicMock()
    fake.github_webhook_secret_id = "/aidlc/dev/github-webhook-secret"  # noqa: S105
    fake.github_bot_login = BOT_LOGIN
    fake.runs_table = "test-runs"
    monkeypatch.setattr("dashboard.routes.webhooks.settings", lambda: fake)

    def fake_secrets() -> Any:
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": SECRET.decode("utf-8")}
        return client

    monkeypatch.setattr("dashboard.routes.webhooks.secrets", fake_secrets)
    yield
    webhook_secret.cache_clear()


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope publish surface uses (direct + start_run)."""
    captured: list[EventEnvelope[Any]] = []

    def capture(envelope: EventEnvelope[Any]) -> None:
        captured.append(envelope)

    monkeypatch.setattr("dashboard.routes.webhooks.publish", capture)

    def fake_start_run(  # noqa: PLR0913
        *,
        project_slug: str,
        intent: str,
        requestor: str,
        requestor_sub: str | None = None,
        target_repo: str | None = None,
        source_issue_url: str | None = None,
        actor_id: str | None = None,
        run_id: RunId | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> tuple[RunId, CorrelationId]:
        rid = run_id or new_run_id()
        cid = correlation_id or new_correlation_id()
        captured.append(
            EventEnvelope[RequestReceived](
                event_id=new_event_id(),
                type="REQUEST.RECEIVED",
                run_id=rid,
                correlation_id=cid,
                actor_id=actor_id or requestor,
                payload=RequestReceived(
                    project_slug=project_slug,
                    intent=intent,
                    requestor=requestor,
                    requestor_sub=requestor_sub,
                    target_repo=target_repo,
                    source_issue_url=source_issue_url,
                ),
            ),
        )
        return rid, cid

    monkeypatch.setattr("dashboard.routes.webhooks.start_run", fake_start_run)
    return captured


def stub_pr_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sk: str = "TASK#T-001",
    project_slug: str = "demo",
    spec_slug: str = "add-healthz",
    correlation_id: str = "cor-1",
    run_id: str = "run-1",
) -> dict[str, Any]:
    """Replace ``lookup_pr`` with a canned row matching ``sk`` shape."""
    row: dict[str, Any] = {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": sk},
        "project_slug": {"S": project_slug},
        "spec_slug": {"S": spec_slug},
        "correlation_id": {"S": correlation_id},
        "spec_s3_prefix": {"S": f"specs/{spec_slug}/"},
    }
    monkeypatch.setattr("dashboard.routes.webhooks.lookup_pr", lambda _url: row)
    return row


def stub_pr_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``lookup_pr`` return ``None`` (PR not tracked)."""
    monkeypatch.setattr("dashboard.routes.webhooks.lookup_pr", lambda _url: None)


def stub_run_by_issue(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str = "run-1",
    project_slug: str = "demo",
) -> None:
    """Replace ``lookup_run_by_issue`` with a canned STATE row."""
    state = {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": "STATE"},
        "project_slug": {"S": project_slug},
        "correlation_id": {"S": "cor-1"},
    }
    monkeypatch.setattr("dashboard.routes.webhooks.lookup_run_by_issue", lambda _url: state)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


async def post_webhook(
    *,
    event_type: str,
    payload: dict[str, Any],
    delivery_id: str = "dlv-1",
) -> dict[str, Any]:
    """Build a fake Request, run the handler, return the JSON response."""
    body = json.dumps(payload).encode("utf-8")
    request = MagicMock()

    async def coro() -> bytes:
        return body

    request.body = coro
    request.headers = {
        "x-hub-signature-256": sign(body),
        "x-github-event": event_type,
        "x-github-delivery": delivery_id,
    }
    return await receive_github_webhook(request)


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def test_verify_signature_accepts_valid() -> None:
    body = b'{"foo":1}'
    verify_signature(body=body, signature_header=sign(body))


def test_verify_signature_rejects_missing_header() -> None:
    with pytest.raises(HTTPException):
        verify_signature(body=b"x", signature_header=None)


def test_verify_signature_rejects_bad_signature() -> None:
    with pytest.raises(HTTPException):
        verify_signature(body=b"x", signature_header="sha256=00")


# ---------------------------------------------------------------------------
# pull_request.closed → SPEC.* / TASK.*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_merged_task_emits_task_approved(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "closed",
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/1",
            "merged": True,
            "merged_by": {"login": "alice"},
        },
    }
    out = await post_webhook(event_type="pull_request", payload=payload)
    assert out["decision"] == "task_approved"
    assert len(captured_events) == 1
    assert captured_events[0].type == "TASK.APPROVED"


@pytest.mark.asyncio
async def test_pr_unmerged_task_emits_task_rejected(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/1", "merged": False},
        "sender": {"login": "alice"},
    }
    await post_webhook(event_type="pull_request", payload=payload)
    assert captured_events[0].type == "TASK.REJECTED"


@pytest.mark.asyncio
async def test_pr_merged_spec_emits_spec_approved(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="STATE")
    payload = {
        "action": "closed",
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/1",
            "merged": True,
            "merged_by": {"login": "alice"},
        },
    }
    await post_webhook(event_type="pull_request", payload=payload)
    assert captured_events[0].type == "SPEC.APPROVED"


@pytest.mark.asyncio
async def test_pr_close_no_match_silently_ignores(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_miss(monkeypatch)
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/99", "merged": True},
    }
    await post_webhook(event_type="pull_request", payload=payload)
    assert captured_events == []


# ---------------------------------------------------------------------------
# pull_request_review → TASK.APPROVED / TASK.ITERATION_REQUESTED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_approved_emits_task_approved(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "submitted",
        "review": {"state": "approved", "user": {"login": "alice"}},
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review", payload=payload)
    assert captured_events[0].type == "TASK.APPROVED"


@pytest.mark.asyncio
async def test_review_changes_requested_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "user": {"login": "alice"},
            "body": "fix this",
            "id": 42,
        },
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review", payload=payload)
    assert captured_events[0].type == "TASK.ITERATION_REQUESTED"


# ---------------------------------------------------------------------------
# pull_request_review_comment → TASK.ITERATION_REQUESTED on bot mention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_comment_with_mention_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "created",
        "comment": {
            "body": f"@{BOT_LOGIN} please look at this",
            "id": 1,
            "user": {"login": "alice"},
            "path": "src/foo.py",
            "line": 42,
            "commit_id": "deadbeef",
        },
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review_comment", payload=payload)
    assert captured_events[0].type == "TASK.ITERATION_REQUESTED"


@pytest.mark.asyncio
async def test_review_comment_no_mention_ignored(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    payload = {
        "action": "created",
        "comment": {"body": "looks good", "id": 1, "user": {"login": "alice"}},
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review_comment", payload=payload)
    assert captured_events == []


# ---------------------------------------------------------------------------
# issue_comment on a PR — magic strings + bot mention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_comment_aidlc_cancel_emits_run_cancel(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc cancel", "user": {"login": "alice"}},
        "issue": {"pull_request": {"html_url": "https://github.com/o/r/pull/1"}},
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events[0].type == "RUN.CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_pr_comment_aidlc_approve_emits_task_approved(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc approve", "user": {"login": "alice"}},
        "issue": {"pull_request": {"html_url": "https://github.com/o/r/pull/1"}},
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events[0].type == "TASK.APPROVED"


@pytest.mark.asyncio
async def test_pr_comment_aidlc_reject_emits_task_rejected(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc reject reason", "user": {"login": "alice"}},
        "issue": {"pull_request": {"html_url": "https://github.com/o/r/pull/1"}},
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events[0].type == "TASK.REJECTED"


@pytest.mark.asyncio
async def test_pr_comment_with_mention_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "created",
        "comment": {
            "body": f"@{BOT_LOGIN} please update tests",
            "id": 7,
            "user": {"login": "alice"},
        },
        "issue": {"pull_request": {"html_url": "https://github.com/o/r/pull/1"}},
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events[0].type == "TASK.ITERATION_REQUESTED"


# ---------------------------------------------------------------------------
# issues — triage triggers + cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_labeled_ready_emits_request_received(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    monkeypatch.setattr("dashboard.routes.webhooks.react_eyes", lambda *args, **kwargs: None)
    payload = {
        "action": "labeled",
        "label": {"name": "aidlc:ready"},
        "issue": {
            "html_url": "https://github.com/o/r/issues/9",
            "title": "Add /version",
            "number": 9,
            "user": {"login": "alice"},
            "labels": [{"name": "aidlc:ready"}],
        },
        "repository": {"full_name": "o/r"},
    }
    await post_webhook(event_type="issues", payload=payload)
    assert captured_events[0].type == "REQUEST.RECEIVED"


@pytest.mark.asyncio
async def test_issue_unassigned_bot_emits_run_cancel(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_run_by_issue(monkeypatch)
    payload = {
        "action": "unassigned",
        "assignee": {"login": BOT_LOGIN},
        "issue": {"html_url": "https://github.com/o/r/issues/9"},
        "sender": {"login": "alice"},
    }
    await post_webhook(event_type="issues", payload=payload)
    assert captured_events[0].type == "RUN.CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_issue_unassigned_non_bot_ignored(
    captured_events: list[EventEnvelope[Any]],
) -> None:
    payload = {
        "action": "unassigned",
        "assignee": {"login": "bob"},
        "issue": {"html_url": "https://github.com/o/r/issues/9"},
    }
    await post_webhook(event_type="issues", payload=payload)
    assert captured_events == []


# ---------------------------------------------------------------------------
# issue_comment on a real issue — /aidlc go, awaiting-response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_comment_aidlc_go_emits_request_received(
    captured_events: list[EventEnvelope[Any]],
) -> None:
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc go", "user": {"login": "alice", "type": "User"}},
        "issue": {
            "html_url": "https://github.com/o/r/issues/9",
            "title": "x",
            "number": 9,
            "user": {"login": "alice"},
            "labels": [],
        },
        "repository": {"full_name": "o/r"},
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events[0].type == "REQUEST.RECEIVED"


@pytest.mark.asyncio
async def test_issue_comment_cancel_emits_run_cancel(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_run_by_issue(monkeypatch)
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc cancel", "user": {"login": "alice", "type": "User"}},
        "issue": {
            "html_url": "https://github.com/o/r/issues/9",
            "labels": [],
        },
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events[0].type == "RUN.CANCEL_REQUESTED"


# ---------------------------------------------------------------------------
# workflow_run failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_run_failure_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    stub_pr_lookup(monkeypatch, sk="TASK#T-001")
    payload = {
        "action": "completed",
        "workflow_run": {
            "name": "ci",
            "conclusion": "failure",
            "head_sha": "deadbeef",
            "html_url": "https://github.com/o/r/actions/runs/1",
            "pull_requests": [{"html_url": "https://github.com/o/r/pull/1", "number": 1}],
        },
    }
    await post_webhook(event_type="workflow_run", payload=payload)
    assert captured_events[0].type == "TASK.ITERATION_REQUESTED"


@pytest.mark.asyncio
async def test_workflow_run_success_ignored(
    captured_events: list[EventEnvelope[Any]],
) -> None:
    payload = {
        "action": "completed",
        "workflow_run": {"conclusion": "success", "pull_requests": []},
    }
    await post_webhook(event_type="workflow_run", payload=payload)
    assert captured_events == []


# ---------------------------------------------------------------------------
# Unknown event type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_event_type_ignored(
    captured_events: list[EventEnvelope[Any]],
) -> None:
    out = await post_webhook(event_type="ping", payload={})
    assert out["ignored"] is True
    assert captured_events == []
