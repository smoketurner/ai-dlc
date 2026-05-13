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
from common.runs import IssueContext
from dashboard.routes.webhooks import (
    build_issue_context,
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
    fake.repo_helper_function_name = "test-repo-helper"
    monkeypatch.setattr("dashboard.routes.webhooks.settings", lambda: fake)

    def fake_secrets() -> Any:
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": SECRET.decode("utf-8")}
        return client

    monkeypatch.setattr("dashboard.routes.webhooks.secrets", fake_secrets)
    # react_eyes makes a live httpx call to GitHub. Replace with a no-op
    # for every test; tests that want to assert on reactions monkeypatch
    # it themselves with a recording stub.
    monkeypatch.setattr("dashboard.routes.webhooks.react_eyes", lambda **_: None)
    # Powertools' idempotency layer needs DynamoDB. We're not exercising
    # idempotency in these tests (start_run is stubbed); disabling here
    # keeps the layer from reaching for the real table.
    monkeypatch.setenv("POWERTOOLS_IDEMPOTENCY_DISABLED", "1")
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
        issue: IssueContext | None = None,
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
                    source_issue_url=issue.issue_url if issue else None,
                ),
            ),
        )
        return rid, cid

    monkeypatch.setattr("dashboard.routes.webhooks.start_run", fake_start_run)
    return captured


def _state_row(
    *,
    project_slug: str = "demo",
    correlation_id: str = "cor-1",
    run_id: str = "run-1",
) -> dict[str, Any]:
    """Build a canned STATE row that ``lookup_pr`` returns for the impl PR."""
    return {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": "STATE"},
        "project_slug": {"S": project_slug},
        "correlation_id": {"S": correlation_id},
    }


def stub_pr_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_slug: str = "demo",
    correlation_id: str = "cor-1",
    run_id: str = "run-1",
) -> dict[str, Any]:
    """Replace ``lookup_pr`` so it returns a canned STATE row."""
    row = _state_row(project_slug=project_slug, correlation_id=correlation_id, run_id=run_id)
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


def stub_check_state(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    """Replace ``get_check_state`` to return ``state`` regardless of args."""
    monkeypatch.setattr(
        "dashboard.routes.webhooks.get_check_state",
        lambda _repo, _pr_number: state,
    )


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
# pull_request.closed → RUN.COMPLETED / RUN.CANCEL_REQUESTED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_merged_emits_run_completed(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Merging the impl PR emits a single ``RUN.COMPLETED``."""
    stub_pr_lookup(monkeypatch)
    payload = {
        "action": "closed",
        "pull_request": {
            "html_url": "https://github.com/o/r/pull/1",
            "merged": True,
            "merged_by": {"login": "alice"},
        },
    }
    out = await post_webhook(event_type="pull_request", payload=payload)
    assert out["decision"] == "run_completed"
    assert [e.type for e in captured_events] == ["RUN.COMPLETED"]
    assert captured_events[0].payload.pr_url == "https://github.com/o/r/pull/1"
    assert captured_events[0].payload.project_slug == "demo"


@pytest.mark.asyncio
async def test_pr_closed_without_merge_emits_run_cancel(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Closing the impl PR without merging cancels the run."""
    stub_pr_lookup(monkeypatch)
    payload = {
        "action": "closed",
        "pull_request": {"html_url": "https://github.com/o/r/pull/1", "merged": False},
        "sender": {"login": "alice"},
    }
    await post_webhook(event_type="pull_request", payload=payload)
    assert len(captured_events) == 1
    envelope = captured_events[0]
    assert envelope.type == "RUN.CANCEL_REQUESTED"
    assert envelope.payload.source == "pr_closed"


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
# pull_request_review → IMPL.ITERATION_REQUESTED on bot mention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_approved_without_mention_ignored(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """A bare PR review approval (no @-mention body) emits nothing."""
    stub_pr_lookup(monkeypatch)
    payload = {
        "action": "submitted",
        "review": {"state": "approved", "user": {"login": "alice"}, "body": "looks good"},
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review", payload=payload)
    assert captured_events == []


@pytest.mark.asyncio
async def test_review_changes_requested_with_mention_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """changes_requested w/ @-mention emits IMPL.ITERATION_REQUESTED."""
    stub_pr_lookup(monkeypatch)
    payload = {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "user": {"login": "alice"},
            "body": f"@{BOT_LOGIN} fix this",
            "id": 42,
        },
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review", payload=payload)
    assert len(captured_events) == 1
    envelope = captured_events[0]
    assert envelope.type == "IMPL.ITERATION_REQUESTED"
    assert envelope.payload.source == "review_changes_requested"
    assert envelope.payload.commenter == "alice"
    assert envelope.payload.pr_url == "https://github.com/o/r/pull/1"


@pytest.mark.asyncio
async def test_review_commented_with_mention_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Plain comment review w/ @-mention emits ``source=issue_comment_mention``."""
    stub_pr_lookup(monkeypatch)
    payload = {
        "action": "submitted",
        "review": {
            "state": "commented",
            "user": {"login": "alice"},
            "body": f"@{BOT_LOGIN} please look at this",
        },
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
    }
    await post_webhook(event_type="pull_request_review", payload=payload)
    assert captured_events[0].type == "IMPL.ITERATION_REQUESTED"
    assert captured_events[0].payload.source == "issue_comment_mention"


# ---------------------------------------------------------------------------
# pull_request_review_comment → IMPL.ITERATION_REQUESTED on bot mention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_comment_with_mention_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """A bot-mention inline review comment emits IMPL.ITERATION_REQUESTED."""
    stub_pr_lookup(monkeypatch)
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
    assert [e.type for e in captured_events] == ["IMPL.ITERATION_REQUESTED"]
    assert captured_events[0].payload.source == "review_comment_mention"


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
# issue_comment on a PR — bot mention only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_comment_without_mention_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Plain PR comments without a bot mention emit nothing."""
    stub_pr_lookup(monkeypatch)
    payload = {
        "action": "created",
        "comment": {"body": "looks fine to me", "user": {"login": "alice"}},
        "issue": {"pull_request": {"html_url": "https://github.com/o/r/pull/1"}},
    }
    await post_webhook(event_type="issue_comment", payload=payload)
    assert captured_events == []


@pytest.mark.asyncio
async def test_pr_comment_with_mention_emits_iteration(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """``@aidlc-bot`` on the impl PR emits ``IMPL.ITERATION_REQUESTED``."""
    stub_pr_lookup(monkeypatch)
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
    assert [e.type for e in captured_events] == ["IMPL.ITERATION_REQUESTED"]
    payload_out = captured_events[0].payload
    assert payload_out.source == "issue_comment_mention"
    assert payload_out.commenter == "alice"


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


@pytest.mark.asyncio
async def test_issue_closed_emits_run_cancel(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Closing an in-flight issue cancels its run."""
    stub_run_by_issue(monkeypatch)
    payload = {
        "action": "closed",
        "issue": {"html_url": "https://github.com/o/r/issues/9"},
        "sender": {"login": "alice"},
    }
    await post_webhook(event_type="issues", payload=payload)
    assert captured_events[0].type == "RUN.CANCEL_REQUESTED"
    assert captured_events[0].payload.source == "issue_closed"


@pytest.mark.asyncio
async def test_issue_closed_without_run_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Closing an issue with no in-flight run emits nothing."""
    monkeypatch.setattr("dashboard.routes.webhooks.lookup_run_by_issue", lambda _: None)
    payload = {
        "action": "closed",
        "issue": {"html_url": "https://github.com/o/r/issues/9"},
        "sender": {"login": "alice"},
    }
    await post_webhook(event_type="issues", payload=payload)
    assert captured_events == []


# ---------------------------------------------------------------------------
# issue_comment on a real issue — @aidlc-bot mention, awaiting-response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_comment_without_mention_is_ignored(
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """Plain issue comments without a bot mention emit nothing."""
    payload = {
        "action": "created",
        "comment": {"body": "thinking about this", "user": {"login": "alice", "type": "User"}},
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
    assert captured_events == []


@pytest.mark.asyncio
async def test_issue_comment_bot_mention_emits_request_received(
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """``@aidlc-bot`` on a non-PR issue mints a fresh triage run."""
    payload = {
        "action": "created",
        "comment": {
            "body": f"@{BOT_LOGIN} please look at this",
            "user": {"login": "alice", "type": "User"},
        },
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
    assert len(captured_events) == 1
    assert captured_events[0].type == "REQUEST.RECEIVED"


@pytest.mark.asyncio
async def test_issue_comment_bot_mention_reacts_on_comment(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """``@aidlc-bot`` on a non-PR issue posts a 👀 on the comment id."""
    reactions: list[dict[str, str]] = []
    monkeypatch.setattr(
        "dashboard.routes.webhooks.react_eyes",
        lambda *, repo, reactions_url: reactions.append(
            {"repo": repo, "url": reactions_url},
        ),
    )
    payload = {
        "action": "created",
        "comment": {
            "id": 4399714948,
            "body": f"@{BOT_LOGIN} please retry",
            "user": {"login": "alice", "type": "User"},
        },
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
    assert len(reactions) == 1
    assert reactions[0]["repo"] == "o/r"
    assert "/issues/comments/4399714948/reactions" in reactions[0]["url"]


@pytest.mark.asyncio
async def test_pr_review_comment_with_mention_reacts_on_comment(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """PR-review (inline diff) comments react on the pulls/comments/{id} URL."""
    stub_pr_lookup(monkeypatch)
    reactions: list[dict[str, str]] = []
    monkeypatch.setattr(
        "dashboard.routes.webhooks.react_eyes",
        lambda *, repo, reactions_url: reactions.append(
            {"repo": repo, "url": reactions_url},
        ),
    )
    payload = {
        "action": "created",
        "comment": {
            "id": 7777,
            "body": f"@{BOT_LOGIN} address this",
            "path": "src/x.py",
            "line": 42,
            "commit_id": "abcdef0",
            "in_reply_to_id": None,
            "user": {"login": "alice"},
        },
        "pull_request": {"html_url": "https://github.com/o/r/pull/1"},
        "repository": {"full_name": "o/r"},
    }
    await post_webhook(
        event_type="pull_request_review_comment",
        payload=payload,
        delivery_id="d-pr-review-comment-1",
    )
    assert len(reactions) == 1
    assert "/pulls/comments/7777/reactions" in reactions[0]["url"]


# ---------------------------------------------------------------------------
# Check-state aggregation — workflow_run / check_run / check_suite
# ---------------------------------------------------------------------------


def _checks_payload(holder_key: str, **overrides: Any) -> dict[str, Any]:
    """Build a payload shaped like a Checks-family GitHub event."""
    holder: dict[str, Any] = {
        "conclusion": "failure",
        "head_sha": "deadbeef",
        "name": "ci",
        "pull_requests": [{"html_url": "https://github.com/o/r/pull/1", "number": 1}],
    }
    holder.update(overrides.pop("holder", {}))
    return {
        "action": "completed",
        holder_key: holder,
        "repository": {"full_name": "o/r", "html_url": "https://github.com/o/r"},
        **overrides,
    }


@pytest.mark.asyncio
async def test_workflow_run_passed_emits_checks_passed(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """When ``get_check_state`` says ``passed`` we emit ``CHECKS.PASSED``."""
    stub_pr_lookup(monkeypatch)
    stub_check_state(monkeypatch, "passed")
    payload = _checks_payload("workflow_run", holder={"conclusion": "success"})
    await post_webhook(event_type="workflow_run", payload=payload)
    assert [e.type for e in captured_events] == ["CHECKS.PASSED"]


@pytest.mark.asyncio
async def test_workflow_run_failed_emits_checks_failed(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """A failing workflow run aggregates to ``CHECKS.FAILED``."""
    stub_pr_lookup(monkeypatch)
    stub_check_state(monkeypatch, "failed")
    payload = _checks_payload("workflow_run")
    await post_webhook(event_type="workflow_run", payload=payload)
    assert [e.type for e in captured_events] == ["CHECKS.FAILED"]
    failed = captured_events[0].payload
    assert failed.failed_workflow_count == 1


@pytest.mark.asyncio
async def test_workflow_run_pending_emits_no_event(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """``pending`` aggregation produces no event."""
    stub_pr_lookup(monkeypatch)
    stub_check_state(monkeypatch, "pending")
    payload = _checks_payload("workflow_run")
    await post_webhook(event_type="workflow_run", payload=payload)
    assert captured_events == []


@pytest.mark.asyncio
async def test_check_run_completed_aggregates_to_checks_event(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """A ``check_run.completed`` triggers the same aggregation path."""
    stub_pr_lookup(monkeypatch)
    stub_check_state(monkeypatch, "passed")
    payload = _checks_payload("check_run", holder={"conclusion": "success"})
    await post_webhook(event_type="check_run", payload=payload)
    assert [e.type for e in captured_events] == ["CHECKS.PASSED"]


@pytest.mark.asyncio
async def test_check_suite_completed_failed_emits_checks_failed(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """A ``check_suite.completed`` with overall-failed state emits CHECKS.FAILED."""
    stub_pr_lookup(monkeypatch)
    stub_check_state(monkeypatch, "failed")
    payload = _checks_payload("check_suite")
    await post_webhook(event_type="check_suite", payload=payload)
    assert [e.type for e in captured_events] == ["CHECKS.FAILED"]


@pytest.mark.asyncio
async def test_check_run_for_unknown_pr_ignored(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventEnvelope[Any]],
) -> None:
    """A check event for a PR not in the runs table emits nothing."""
    stub_pr_miss(monkeypatch)
    stub_check_state(monkeypatch, "failed")
    payload = _checks_payload("check_run")
    await post_webhook(event_type="check_run", payload=payload)
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


# ---------------------------------------------------------------------------
# build_issue_context — comment-body forwarding
# ---------------------------------------------------------------------------


def test_build_issue_context_no_comment_payload() -> None:
    """``issues`` triggers (no comment payload) leave the comment fields empty."""
    ctx = build_issue_context(
        issue_payload={"number": 9, "title": "x", "body": "details"},
        source_issue_url="https://github.com/o/r/issues/9",
    )
    assert ctx is not None
    assert ctx.triggering_comment_body == ""
    assert ctx.triggering_commenter == ""


def test_build_issue_context_forwards_comment_body_and_commenter() -> None:
    """``issue_comment`` triggers carry the comment body + commenter login."""
    ctx = build_issue_context(
        issue_payload={"number": 34, "title": "x", "body": "details"},
        source_issue_url="https://github.com/o/r/issues/34",
        comment_payload={
            "body": "@aidlc-bot create issues for the highest-impact items",
            "user": {"login": "jplock"},
        },
    )
    assert ctx is not None
    assert ctx.triggering_comment_body == ("@aidlc-bot create issues for the highest-impact items")
    assert ctx.triggering_commenter == "jplock"


def test_build_issue_context_strips_comment_whitespace() -> None:
    ctx = build_issue_context(
        issue_payload={"number": 9, "title": "x", "body": ""},
        source_issue_url="https://github.com/o/r/issues/9",
        comment_payload={
            "body": f"   @{BOT_LOGIN} please retry  \n",
            "user": {"login": "alice"},
        },
    )
    assert ctx is not None
    assert ctx.triggering_comment_body == f"@{BOT_LOGIN} please retry"


def test_build_issue_context_returns_none_without_issue_url() -> None:
    assert (
        build_issue_context(
            issue_payload={"number": 1},
            source_issue_url="",
        )
        is None
    )
