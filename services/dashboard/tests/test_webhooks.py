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
    build_iteration_base,
    decision_from_comment,
    decision_from_pr_close,
    decision_from_review,
    iteration_from_pr_comment,
    iteration_from_review,
    iteration_from_review_comment,
    iteration_from_workflow_run,
    parse_cancellation,
    parse_decision,
    parse_iteration,
    parse_iteration_meta,
    parse_run_meta,
    parse_triage,
    task_id_from_gate_ref,
    triage_from_issue_comment,
    triage_from_issues,
    verify_signature,
    webhook_secret,
)

SECRET = b"super-secret"
BOT_LOGIN = "aidlc-bot"


@pytest.fixture(autouse=True)
def patch_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the cached secret without going to Secrets Manager."""
    webhook_secret.cache_clear()

    fake_settings = MagicMock()
    fake_settings.github_webhook_secret_id = "/aidlc/dev/github-webhook-secret"  # noqa: S105
    fake_settings.github_bot_login = BOT_LOGIN
    monkeypatch.setattr("dashboard.routes.webhooks.settings", lambda: fake_settings)

    def fake_secrets() -> Any:
        client = MagicMock()
        client.get_secret_value.return_value = {"SecretString": SECRET.decode("utf-8")}
        return client

    monkeypatch.setattr("dashboard.routes.webhooks.secrets", fake_secrets)
    yield
    webhook_secret.cache_clear()


@pytest.fixture
def disable_bot_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the autouse fixture's bot login to test the disabled-trigger case."""
    fake_settings = MagicMock()
    fake_settings.github_webhook_secret_id = "/aidlc/dev/github-webhook-secret"  # noqa: S105
    fake_settings.github_bot_login = ""
    monkeypatch.setattr("dashboard.routes.webhooks.settings", lambda: fake_settings)


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


def test_decision_from_review_changes_requested_no_longer_rejects() -> None:
    """changes_requested reviews now route to iteration_reactor instead of HITL reject."""
    payload = {
        "review": {"state": "changes_requested", "user": {"login": "alice"}, "body": "nope"},
        "pull_request": {"body": "_run_id: r1_\ngate_ref: spec"},
    }
    assert decision_from_review(payload) is None


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


# --- Triage routing -------------------------------------------------------


def issue_payload(
    *,
    action: str,
    labels: list[str],
    pull_request: bool = False,
    label_added: str | None = None,
    body: str = "Please add a /version endpoint.",
) -> dict[str, Any]:
    """Build a GitHub ``issues`` webhook payload with the bits triage cares about."""
    issue: dict[str, Any] = {
        "number": 7,
        "html_url": "https://github.com/o/r/issues/7",
        "title": "Add /version",
        "body": body,
        "labels": [{"name": name} for name in labels],
        "user": {"login": "alice"},
    }
    if pull_request:
        issue["pull_request"] = {"url": "..."}
    payload: dict[str, Any] = {
        "action": action,
        "issue": issue,
        "repository": {"full_name": "o/r"},
    }
    if label_added is not None:
        payload["label"] = {"name": label_added}
    return payload


def test_triage_from_issues_opened_with_ready_label() -> None:
    payload = issue_payload(action="opened", labels=["aidlc:ready", "enhancement"])

    envelope = triage_from_issues(payload)

    assert envelope is not None
    assert envelope["repo"] == "o/r"
    assert envelope["issue_number"] == 7
    assert envelope["issue_url"] == "https://github.com/o/r/issues/7"
    assert "aidlc:ready" in envelope["labels"]


def test_triage_from_issues_labeled_event_only_fires_on_ready_label() -> None:
    on_ready = issue_payload(
        action="labeled",
        labels=["aidlc:ready"],
        label_added="aidlc:ready",
    )
    on_other = issue_payload(
        action="labeled",
        labels=["bug"],
        label_added="bug",
    )

    assert triage_from_issues(on_ready) is not None
    assert triage_from_issues(on_other) is None


def test_triage_from_issues_skips_terminal_labels() -> None:
    payload = issue_payload(action="opened", labels=["aidlc:ready", "aidlc:in-progress"])
    assert triage_from_issues(payload) is None


def test_triage_from_issue_comment_picks_up_aidlc_go() -> None:
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc go please", "user": {"login": "alice"}},
        "issue": {
            "number": 7,
            "html_url": "https://github.com/o/r/issues/7",
            "title": "Add /version",
            "body": "context",
            "labels": [],
            "user": {"login": "alice"},
        },
        "repository": {"full_name": "o/r"},
    }

    envelope = triage_from_issue_comment(payload)

    assert envelope is not None
    assert envelope["issue_number"] == 7


def test_triage_from_issue_comment_ignores_pr_comments() -> None:
    payload = {
        "action": "created",
        "comment": {"body": "/aidlc go", "user": {"login": "alice"}},
        "issue": {
            "number": 7,
            "html_url": "https://github.com/o/r/issues/7",
            "title": "x",
            "body": "x",
            "labels": [],
            "user": {"login": "alice"},
            "pull_request": {"url": "..."},
        },
        "repository": {"full_name": "o/r"},
    }

    assert triage_from_issue_comment(payload) is None


def test_parse_triage_routes_only_relevant_event_types() -> None:
    issues_payload = issue_payload(action="opened", labels=["aidlc:ready"])
    assert parse_triage("issues", issues_payload) is not None
    assert parse_triage("ping", {}) is None
    assert parse_triage("pull_request_review", {}) is None


def assigned_payload(*, assignee_login: str, labels: list[str] | None = None) -> dict[str, Any]:
    """Build an ``issues.assigned`` webhook payload for the given assignee."""
    payload = issue_payload(action="assigned", labels=labels or [])
    payload["assignee"] = {"login": assignee_login}
    return payload


def test_triage_from_issues_assigned_to_bot_triggers() -> None:
    envelope = triage_from_issues(assigned_payload(assignee_login=BOT_LOGIN))

    assert envelope is not None
    assert envelope["repo"] == "o/r"
    assert envelope["issue_number"] == 7


def test_triage_from_issues_assigned_to_human_ignored() -> None:
    assert triage_from_issues(assigned_payload(assignee_login="alice")) is None


def test_triage_from_issues_assigned_disabled_when_bot_login_empty(
    disable_bot_login: None,
) -> None:
    assert triage_from_issues(assigned_payload(assignee_login=BOT_LOGIN)) is None


def test_triage_from_issues_assigned_skips_terminal_labels() -> None:
    payload = assigned_payload(assignee_login=BOT_LOGIN, labels=["aidlc:in-progress"])
    assert triage_from_issues(payload) is None


def comment_payload(
    *,
    body: str,
    labels: list[str],
    user_login: str = "alice",
    user_type: str | None = None,
) -> dict[str, Any]:
    """Build an ``issue_comment.created`` payload on a real (non-PR) issue."""
    user: dict[str, Any] = {"login": user_login}
    if user_type is not None:
        user["type"] = user_type
    return {
        "action": "created",
        "comment": {"body": body, "user": user},
        "issue": {
            "number": 7,
            "html_url": "https://github.com/o/r/issues/7",
            "title": "Add /version",
            "body": "context",
            "labels": [{"name": name} for name in labels],
            "user": {"login": "alice"},
        },
        "repository": {"full_name": "o/r"},
    }


def test_triage_from_issue_comment_resumes_ask_loop_on_human_reply() -> None:
    payload = comment_payload(
        body="The status code on auth failure should be 401.",
        labels=["aidlc:awaiting-response"],
    )

    envelope = triage_from_issue_comment(payload)

    assert envelope is not None
    assert envelope["prior_human_comments"] == ["The status code on auth failure should be 401."]
    assert envelope["prior_triage_count"] == 1


def test_triage_from_issue_comment_ask_loop_ignores_bot_replies() -> None:
    payload = comment_payload(
        body="anything",
        labels=["aidlc:awaiting-response"],
        user_login=BOT_LOGIN,
        user_type="Bot",
    )

    assert triage_from_issue_comment(payload) is None


def test_triage_from_issue_comment_aidlc_go_still_works_without_awaiting_label() -> None:
    payload = comment_payload(body="/aidlc go please", labels=[])

    envelope = triage_from_issue_comment(payload)

    assert envelope is not None
    # Fresh round, not a resume — no prior context attached.
    assert "prior_human_comments" not in envelope
    assert "prior_triage_count" not in envelope


# --- pull_request.closed → DECIDE ----------------------------------------


def pr_closed_payload(
    *,
    merged: bool,
    body: str = "_run_id: r1_\ngate_ref: task:T-001",
) -> dict[str, Any]:
    """Build a ``pull_request.closed`` webhook payload with run_id/gate_ref markers."""
    return {
        "action": "closed",
        "pull_request": {
            "body": body,
            "merged": merged,
            "merged_by": {"login": "carol"} if merged else None,
            "user": {"login": "ai-dlc-bot"},
        },
        "sender": {"login": "dave" if not merged else "carol"},
    }


def test_decision_from_pr_close_merged_resolves_as_approve() -> None:
    decision = decision_from_pr_close(pr_closed_payload(merged=True))

    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["reviewer"] == "carol"
    assert decision["gate_ref"] == "task:T-001"
    assert decision["reason"] == "PR merged"


def test_decision_from_pr_close_unmerged_resolves_as_reject() -> None:
    decision = decision_from_pr_close(pr_closed_payload(merged=False))

    assert decision is not None
    assert decision["decision"] == "reject"
    assert decision["reviewer"] == "dave"
    assert decision["reason"] == "PR closed without merge"


def test_decision_from_pr_close_ignores_pr_without_run_meta() -> None:
    payload = pr_closed_payload(merged=True, body="just a regular PR body")
    assert decision_from_pr_close(payload) is None


def test_decision_from_pr_close_ignores_non_close_action() -> None:
    payload = pr_closed_payload(merged=False)
    payload["action"] = "opened"
    assert decision_from_pr_close(payload) is None


def test_parse_decision_routes_pull_request_close() -> None:
    assert parse_decision("pull_request", pr_closed_payload(merged=True)) is not None


# --- issues.unassigned → CANCEL_RUN --------------------------------------


def unassign_payload(
    *,
    unassignee_login: str = BOT_LOGIN,
    issue_url: str = "https://github.com/o/r/issues/7",
    sender_login: str = "alice",
) -> dict[str, Any]:
    """Build an ``issues.unassigned`` webhook payload."""
    return {
        "action": "unassigned",
        "assignee": {"login": unassignee_login},
        "issue": {"html_url": issue_url, "number": 7, "title": "x"},
        "repository": {"full_name": "o/r"},
        "sender": {"login": sender_login},
    }


@pytest.fixture
def stub_ddb_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``ddb()`` with a MagicMock whose query return is configurable."""
    fake = MagicMock()
    fake.query.return_value = {"Items": []}
    monkeypatch.setattr("dashboard.routes.webhooks.ddb", lambda: fake)
    return {"client": fake}


def test_parse_cancellation_returns_cancel_payload_when_run_found(
    monkeypatch: pytest.MonkeyPatch,
    stub_ddb_lookup: dict[str, Any],
) -> None:
    fake_settings = MagicMock()
    fake_settings.github_webhook_secret_id = "/aidlc/dev/github-webhook-secret"  # noqa: S105
    fake_settings.github_bot_login = BOT_LOGIN
    fake_settings.runs_table = "runs-table"
    monkeypatch.setattr("dashboard.routes.webhooks.settings", lambda: fake_settings)
    stub_ddb_lookup["client"].query.return_value = {"Items": [{"pk": {"S": "RUN#run-7"}}]}

    out = parse_cancellation("issues", unassign_payload())

    assert out is not None
    assert out["op"] == "CANCEL_RUN"
    assert out["run_id"] == "run-7"
    assert out["reviewer"] == "alice"
    assert "https://github.com/o/r/issues/7" in out["reason"]


def test_parse_cancellation_returns_none_when_no_run_indexed(
    stub_ddb_lookup: dict[str, Any],
) -> None:
    # Default stub returns no Items.
    assert parse_cancellation("issues", unassign_payload()) is None


def test_parse_cancellation_ignores_non_bot_unassignee(
    stub_ddb_lookup: dict[str, Any],
) -> None:
    payload = unassign_payload(unassignee_login="alice")
    assert parse_cancellation("issues", payload) is None


def test_parse_cancellation_disabled_when_bot_login_unset(
    disable_bot_login: None,
    stub_ddb_lookup: dict[str, Any],
) -> None:
    assert parse_cancellation("issues", unassign_payload()) is None


def test_parse_cancellation_ignores_non_unassign_actions(
    stub_ddb_lookup: dict[str, Any],
) -> None:
    payload = unassign_payload()
    payload["action"] = "assigned"
    assert parse_cancellation("issues", payload) is None


# ---------------------------------------------------------------------------
# Iteration trigger parsers
# ---------------------------------------------------------------------------

PR_BODY_FOOTER = (
    "_run_id: r1_  ·  _correlation_id: c1_  ·  "
    "_project: demo_  ·  _spec: `docs/specs/add-healthz/`_\n\n"
    "gate_ref: task:T-001"
)


def pr_envelope(*, body: str = PR_BODY_FOOTER) -> dict[str, Any]:
    return {
        "body": body,
        "html_url": "https://github.com/owner/repo/pull/42",
        "number": 42,
        "head": {"sha": "abcdef0123"},
    }


def repo_envelope() -> dict[str, Any]:
    return {"full_name": "owner/repo", "html_url": "https://github.com/owner/repo"}


def test_parse_iteration_meta_extracts_all_fields() -> None:
    extras = parse_iteration_meta(PR_BODY_FOOTER)
    assert extras["project_slug"] == "demo"
    assert extras["spec_slug"] == "add-healthz"
    assert extras["correlation_id"] == "c1"


def test_parse_iteration_meta_returns_none_when_missing() -> None:
    extras = parse_iteration_meta("no markers here")
    assert extras["project_slug"] is None
    assert extras["spec_slug"] is None
    assert extras["correlation_id"] is None


def test_task_id_from_gate_ref_extracts() -> None:
    assert task_id_from_gate_ref("task:T-001") == "T-001"


def test_task_id_from_gate_ref_rejects_non_task_gate() -> None:
    assert task_id_from_gate_ref("spec") is None


def test_build_iteration_base_minimal_path() -> None:
    payload = {"repository": repo_envelope()}
    base = build_iteration_base(pr_envelope(), payload, delivery_id="d-1")
    assert base is not None
    assert base["task_id"] == "T-001"
    assert base["project_slug"] == "demo"
    assert base["spec_slug"] == "add-healthz"
    assert base["spec_s3_prefix"] == "specs/add-healthz/"
    assert base["target_repo"] == "owner/repo"
    assert base["pr_url"] == "https://github.com/owner/repo/pull/42"
    assert base["pr_number"] == 42
    assert base["head_sha"] == "abcdef0123"
    assert base["delivery_id"] == "d-1"


def test_build_iteration_base_returns_none_when_footer_missing() -> None:
    payload = {"repository": repo_envelope()}
    assert build_iteration_base(pr_envelope(body="no footer"), payload, delivery_id="d-1") is None


def test_build_iteration_base_returns_none_when_gate_ref_isnt_task() -> None:
    body = (
        "_run_id: r1_  ·  _correlation_id: c1_  ·  "
        "_project: demo_  ·  _spec: `docs/specs/add-healthz/`_\n\n"
        "gate_ref: spec"
    )
    payload = {"repository": repo_envelope()}
    assert build_iteration_base(pr_envelope(body=body), payload, delivery_id="d-1") is None


def test_iteration_from_review_changes_requested_builds_trigger() -> None:
    payload = {
        "review": {
            "id": 99,
            "state": "changes_requested",
            "user": {"login": "alice"},
            "body": "Fix the null check.",
        },
        "pull_request": pr_envelope(),
        "repository": repo_envelope(),
    }
    out = iteration_from_review(payload, delivery_id="d-1")
    assert out is not None
    assert out["trigger_kind"] == "review_changes_requested"
    assert out["trigger_payload"]["review_id"] == 99
    assert out["trigger_payload"]["reviewer"] == "alice"


def test_iteration_from_review_skips_approved() -> None:
    payload = {
        "review": {"state": "approved", "user": {"login": "alice"}},
        "pull_request": pr_envelope(),
        "repository": repo_envelope(),
    }
    assert iteration_from_review(payload, delivery_id="d-1") is None


def test_iteration_from_review_comment_requires_bot_mention() -> None:
    payload = {
        "action": "created",
        "comment": {
            "id": 7,
            "path": "src/x.py",
            "line": 42,
            "commit_id": "abcdef0123",
            "body": "Just a regular comment.",  # no @-mention
            "user": {"login": "alice"},
        },
        "pull_request": pr_envelope(),
        "repository": repo_envelope(),
    }
    assert iteration_from_review_comment(payload, delivery_id="d-1") is None


def test_iteration_from_review_comment_with_mention_builds_trigger() -> None:
    payload = {
        "action": "created",
        "comment": {
            "id": 7,
            "path": "src/x.py",
            "line": 42,
            "commit_id": "abcdef0123",
            "body": f"@{BOT_LOGIN} please fix this null-check",
            "user": {"login": "alice"},
        },
        "pull_request": pr_envelope(),
        "repository": repo_envelope(),
    }
    out = iteration_from_review_comment(payload, delivery_id="d-1")
    assert out is not None
    assert out["trigger_kind"] == "review_comment_mention"
    assert out["trigger_payload"]["comment_id"] == 7
    assert out["trigger_payload"]["path"] == "src/x.py"


def test_iteration_from_pr_comment_requires_bot_mention_on_pr() -> None:
    payload = {
        "action": "created",
        "comment": {
            "id": 12,
            "body": f"@{BOT_LOGIN} take another look",
            "user": {"login": "alice"},
        },
        "issue": {
            "pull_request": {"html_url": "https://github.com/owner/repo/pull/42"},
            "body": PR_BODY_FOOTER,
            "number": 42,
        },
        "repository": repo_envelope(),
    }
    out = iteration_from_pr_comment(payload, delivery_id="d-1")
    assert out is not None
    assert out["trigger_kind"] == "issue_comment_mention"


def test_iteration_from_pr_comment_aidlc_magic_string_takes_precedence() -> None:
    """A `/aidlc approve` comment that ALSO @-mentions the bot routes to HITL, not iteration."""
    payload = {
        "action": "created",
        "comment": {
            "id": 13,
            "body": f"/aidlc approve — @{BOT_LOGIN}",
            "user": {"login": "alice"},
        },
        "issue": {"pull_request": {}, "body": PR_BODY_FOOTER},
        "repository": repo_envelope(),
    }
    assert iteration_from_pr_comment(payload, delivery_id="d-1") is None


def test_iteration_from_pr_comment_skips_non_pr_comments() -> None:
    payload = {
        "action": "created",
        "comment": {"id": 1, "body": f"@{BOT_LOGIN}", "user": {"login": "alice"}},
        "issue": {"body": PR_BODY_FOOTER},  # no pull_request key
        "repository": repo_envelope(),
    }
    assert iteration_from_pr_comment(payload, delivery_id="d-1") is None


def test_iteration_from_workflow_run_failure_builds_trigger() -> None:
    payload = {
        "action": "completed",
        "workflow_run": {
            "name": "CI / test",
            "conclusion": "failure",
            "html_url": "https://github.com/owner/repo/actions/runs/1",
            "head_sha": "deadbeef00",
            "pull_requests": [
                {"number": 42, "html_url": "https://github.com/owner/repo/pull/42",
                 "body": PR_BODY_FOOTER},
            ],
        },
        "repository": repo_envelope(),
    }
    out = iteration_from_workflow_run(payload, delivery_id="d-1")
    assert out is not None
    assert out["trigger_kind"] == "ci_failure"
    assert out["trigger_payload"]["workflow_name"] == "CI / test"
    assert out["head_sha"] == "deadbeef00"


def test_iteration_from_workflow_run_skips_success() -> None:
    payload = {
        "action": "completed",
        "workflow_run": {
            "name": "CI",
            "conclusion": "success",
            "head_sha": "deadbeef00",
            "pull_requests": [{"number": 42, "html_url": "https://github.com/x/y/pull/42",
                               "body": PR_BODY_FOOTER}],
        },
        "repository": repo_envelope(),
    }
    assert iteration_from_workflow_run(payload, delivery_id="d-1") is None


def test_iteration_from_workflow_run_skips_when_no_pr_attached() -> None:
    payload = {
        "action": "completed",
        "workflow_run": {
            "name": "CI",
            "conclusion": "failure",
            "head_sha": "deadbeef00",
            "pull_requests": [],
        },
        "repository": repo_envelope(),
    }
    assert iteration_from_workflow_run(payload, delivery_id="d-1") is None


def test_parse_iteration_routes_correctly() -> None:
    review_payload = {
        "review": {"id": 99, "state": "changes_requested", "user": {"login": "alice"}, "body": "x"},
        "pull_request": pr_envelope(),
        "repository": repo_envelope(),
    }
    out = parse_iteration("pull_request_review", review_payload, delivery_id="d-1")
    assert out is not None
    assert out["trigger_kind"] == "review_changes_requested"


def test_parse_iteration_returns_none_for_unknown_event() -> None:
    out = parse_iteration("ping", {}, delivery_id="d-1")
    assert out is None


def test_parse_iteration_returns_none_when_no_delivery_id() -> None:
    """Without an ``X-GitHub-Delivery`` header we can't dedupe — skip the trigger."""
    review_payload = {
        "review": {"id": 99, "state": "changes_requested", "user": {"login": "alice"}, "body": "x"},
        "pull_request": pr_envelope(),
        "repository": repo_envelope(),
    }
    assert parse_iteration("pull_request_review", review_payload, delivery_id="") is None
