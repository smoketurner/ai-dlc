"""POST /webhooks/github — HMAC-verified GitHub webhook receiver.

GitHub's webhook hits this route with no Cognito JWT (the ALB listener rule
lets it through unauthenticated). We verify the HMAC-SHA256 signature against
the webhook secret stored in Secrets Manager, then dispatch on event type:

* HITL gates (forwarded to the ``hitl_handler`` Lambda's ``DECIDE`` op):
    * pull_request_review.submitted state=approved          → approve
    * pull_request_review.submitted state=changes_requested → reject
    * issue_comment.created on a PR with body containing
      ``/aidlc approve`` or ``/aidlc reject <reason>``      → approve / reject

* Triage (forwarded to the ``triage_dispatcher`` Lambda):
    * issues.opened with the ``aidlc:ready`` label
    * issues.labeled when the new label is ``aidlc:ready``
    * issues.assigned when the bot login is the new assignee
    * issue_comment.created on a real issue (not a PR) with
      ``/aidlc go``                                          → triage
    * issue_comment.created on an issue carrying
      ``aidlc:awaiting-response`` (resumes the *ask* loop;
      ``prior_human_comments`` + ``prior_triage_count`` are populated
      from the issue thread so the agent has the conversation history).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from functools import cache
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status

from dashboard.deps import lambda_client, secrets, settings

router = APIRouter()
logger = structlog.get_logger()

APPROVE_RE = re.compile(r"/aidlc\s+approve\b", re.IGNORECASE)
REJECT_RE = re.compile(r"/aidlc\s+reject\s*(.*)", re.IGNORECASE)
GO_RE = re.compile(r"/aidlc\s+go\b", re.IGNORECASE)

READY_LABEL = "aidlc:ready"
AWAITING_RESPONSE_LABEL = "aidlc:awaiting-response"
TERMINAL_LABELS = frozenset({"aidlc:in-progress", "aidlc:deferred", "aidlc:declined"})


@cache
def webhook_secret() -> bytes:
    """Fetch + cache the GitHub webhook signing secret."""
    secret_id = settings().github_webhook_secret_id
    resp = secrets().get_secret_value(SecretId=secret_id)  # ty: ignore[unresolved-attribute]
    payload = resp.get("SecretString") or resp.get("SecretBinary") or ""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return payload


def verify_signature(*, body: bytes, signature_header: str | None) -> None:
    """Constant-time HMAC verification; raises 401 on mismatch."""
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature.")
    expected = "sha256=" + hmac.new(webhook_secret(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature.")


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def receive_github_webhook(request: Request) -> dict[str, Any]:
    """Verify HMAC and route to hitl_handler (HITL gate) or triage_dispatcher."""
    body = await request.body()
    verify_signature(body=body, signature_header=request.headers.get("x-hub-signature-256"))
    event_type = request.headers.get("x-github-event", "")
    payload: dict[str, Any] = json.loads(body) if body else {}

    decision = parse_decision(event_type, payload)
    if decision is not None:
        invoke_hitl_decide(decision)
        return {"ok": True, "decision": decision["decision"], "gate_ref": decision["gate_ref"]}

    triage = parse_triage(event_type, payload)
    if triage is not None:
        invoke_triage(triage)
        return {"ok": True, "triage": triage["issue_url"]}

    return {"ok": True, "ignored": True, "event": event_type}


def parse_decision(event_type: str, payload: dict[str, Any]) -> dict[str, str] | None:
    """Return a DECIDE payload, or ``None`` if this webhook doesn't approve a gate."""
    if event_type == "pull_request_review":
        return decision_from_review(payload)
    if event_type == "issue_comment":
        return decision_from_comment(payload)
    return None


def decision_from_review(payload: dict[str, Any]) -> dict[str, str] | None:
    """Parse a ``pull_request_review.submitted`` payload."""
    review = payload.get("review", {})
    state = review.get("state")
    if state not in {"approved", "changes_requested"}:
        return None
    pr = payload.get("pull_request", {})
    run_id, gate_ref = parse_run_meta(pr.get("body", "") or "")
    if run_id is None or gate_ref is None:
        return None
    decision = "approve" if state == "approved" else "reject"
    return {
        "op": "DECIDE",
        "run_id": run_id,
        "gate_ref": gate_ref,
        "decision": decision,
        "reviewer": review.get("user", {}).get("login", "unknown"),
        "reason": review.get("body") or "",
    }


def decision_from_comment(payload: dict[str, Any]) -> dict[str, str] | None:
    """Parse an ``issue_comment.created`` payload looking for `/aidlc <decision>`."""
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment", {})
    body = comment.get("body", "") or ""
    pr = payload.get("issue", {}).get("pull_request")
    if pr is None:
        return None
    issue_body = payload.get("issue", {}).get("body", "") or ""
    run_id, gate_ref = parse_run_meta(issue_body)
    if run_id is None or gate_ref is None:
        return None
    if APPROVE_RE.search(body):
        return {
            "op": "DECIDE",
            "run_id": run_id,
            "gate_ref": gate_ref,
            "decision": "approve",
            "reviewer": comment.get("user", {}).get("login", "unknown"),
        }
    match = REJECT_RE.search(body)
    if match:
        return {
            "op": "DECIDE",
            "run_id": run_id,
            "gate_ref": gate_ref,
            "decision": "reject",
            "reviewer": comment.get("user", {}).get("login", "unknown"),
            "reason": match.group(1).strip() or "rejected via /aidlc",
        }
    return None


RUN_META_RE = re.compile(r"_run_id:\s*([\w-]+)_.*?gate_ref:\s*([\w:.-]+)", re.DOTALL)


def parse_run_meta(body: str) -> tuple[str | None, str | None]:
    """Pull ``run_id`` and ``gate_ref`` out of the PR/issue body footer."""
    match = RUN_META_RE.search(body)
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def invoke_hitl_decide(payload: dict[str, str]) -> None:
    """Synchronously invoke the hitl_handler Lambda with the DECIDE op."""
    lambda_client().invoke(
        FunctionName=settings().hitl_handler_function,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def parse_triage(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a triage envelope, or ``None`` if this webhook isn't a triage trigger."""
    if event_type == "issues":
        return triage_from_issues(payload)
    if event_type == "issue_comment":
        return triage_from_issue_comment(payload)
    return None


def triage_from_issues(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an ``issues`` webhook (action=opened/labeled/assigned) for triage candidates.

    ``assigned`` is the autonomous trigger — when the configured bot login
    is added as an assignee, we route the issue to triage just like the
    label-based path. The bot login is configurable via
    ``AIDLC_GITHUB_BOT_LOGIN``; if unset, the assigned trigger is disabled.
    """
    action = payload.get("action")
    issue = payload.get("issue", {})
    if not issue or not issues_action_is_trigger(action, payload):
        return None
    label_names = {label.get("name") for label in issue.get("labels", []) if label.get("name")}
    if label_names & TERMINAL_LABELS:
        return None
    return triage_envelope(payload.get("repository", {}), issue, list(label_names))


def issues_action_is_trigger(action: str | None, payload: dict[str, Any]) -> bool:
    """``True`` if this ``issues`` action should route to triage."""
    issue = payload.get("issue", {})
    label_names = {label.get("name") for label in issue.get("labels", []) if label.get("name")}
    if action == "opened":
        return READY_LABEL in label_names
    if action == "labeled":
        return payload.get("label", {}).get("name") == READY_LABEL
    if action == "assigned":
        bot_login = settings().github_bot_login
        if not bot_login:
            return False
        return (payload.get("assignee") or {}).get("login", "") == bot_login
    return False


def triage_from_issue_comment(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an ``issue_comment.created`` for the two issue-side triage triggers.

    Two paths:

    * ``/aidlc go`` magic-string on a non-terminal issue starts a fresh
      triage round (the historical entry-point).
    * Any human comment on an issue carrying ``aidlc:awaiting-response``
      resumes the *ask* loop — we populate ``prior_human_comments`` +
      ``prior_triage_count`` from the GitHub payload so the triage agent
      sees the full conversation history when re-classifying.
    """
    if payload.get("action") != "created":
        return None
    issue = payload.get("issue", {})
    if issue.get("pull_request") is not None:
        return None
    label_names = [label.get("name", "") for label in issue.get("labels", [])]
    if set(label_names) & TERMINAL_LABELS:
        return None
    body = (payload.get("comment", {}).get("body") or "").strip()
    if AWAITING_RESPONSE_LABEL in label_names and is_human_comment(payload.get("comment", {})):
        return triage_envelope(
            payload.get("repository", {}),
            issue,
            label_names,
            prior_human_comments=[body] if body else [],
            prior_triage_count=count_prior_triage_rounds(label_names),
        )
    if GO_RE.search(body):
        return triage_envelope(payload.get("repository", {}), issue, label_names)
    return None


def is_human_comment(comment: dict[str, Any]) -> bool:
    """``True`` for human commenters; ``False`` for ``Bot`` users / aidlc bot."""
    user = comment.get("user") or {}
    if user.get("type") == "Bot":
        return False
    bot_login = settings().github_bot_login
    return not (bot_login and user.get("login") == bot_login)


def count_prior_triage_rounds(label_names: list[str]) -> int:
    """Estimate prior triage rounds. v1: 1 if awaiting-response label is on, else 0."""
    return 1 if AWAITING_RESPONSE_LABEL in label_names else 0


def triage_envelope(
    repository: dict[str, Any],
    issue: dict[str, Any],
    labels: list[str],
    *,
    prior_human_comments: list[str] | None = None,
    prior_triage_count: int = 0,
) -> dict[str, Any] | None:
    """Shape the GitHub payload into the Triage Lambda's input contract."""
    repo = repository.get("full_name")
    issue_url = issue.get("html_url")
    issue_number = issue.get("number")
    title = issue.get("title")
    if not (repo and issue_url and isinstance(issue_number, int) and title):
        return None
    envelope: dict[str, Any] = {
        "repo": repo,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "title": title,
        "body": issue.get("body") or "",
        "labels": labels,
        "user": (issue.get("user") or {}).get("login", ""),
    }
    if prior_human_comments:
        envelope["prior_human_comments"] = prior_human_comments
    if prior_triage_count:
        envelope["prior_triage_count"] = prior_triage_count
    return envelope


def invoke_triage(payload: dict[str, Any]) -> None:
    """Asynchronously invoke the triage_dispatcher Lambda."""
    lambda_client().invoke(
        FunctionName=settings().triage_dispatcher_function,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
