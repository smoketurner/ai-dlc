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
    * issue_comment.created on a real issue (not a PR) with
      ``/aidlc go``                                          → triage
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
    """Parse an ``issues`` webhook (action=opened or labeled) for triage candidates."""
    action = payload.get("action")
    if action not in {"opened", "labeled"}:
        return None
    issue = payload.get("issue", {})
    if not issue:
        return None
    label_names = {label.get("name") for label in issue.get("labels", []) if label.get("name")}
    if action == "labeled":
        added = payload.get("label", {}).get("name")
        if added != READY_LABEL:
            return None
    elif READY_LABEL not in label_names:
        return None
    if label_names & TERMINAL_LABELS:
        return None
    return triage_envelope(payload.get("repository", {}), issue, list(label_names))


def triage_from_issue_comment(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an ``issue_comment.created`` for ``/aidlc go`` on a real issue."""
    if payload.get("action") != "created":
        return None
    issue = payload.get("issue", {})
    if issue.get("pull_request") is not None:
        return None
    body = (payload.get("comment", {}).get("body") or "").strip()
    if not GO_RE.search(body):
        return None
    label_names = [label.get("name", "") for label in issue.get("labels", [])]
    if set(label_names) & TERMINAL_LABELS:
        return None
    return triage_envelope(payload.get("repository", {}), issue, label_names)


def triage_envelope(
    repository: dict[str, Any],
    issue: dict[str, Any],
    labels: list[str],
) -> dict[str, Any] | None:
    """Shape the GitHub payload into the Triage Lambda's input contract."""
    repo = repository.get("full_name")
    issue_url = issue.get("html_url")
    issue_number = issue.get("number")
    title = issue.get("title")
    if not (repo and issue_url and isinstance(issue_number, int) and title):
        return None
    return {
        "repo": repo,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "title": title,
        "body": issue.get("body") or "",
        "labels": labels,
        "user": (issue.get("user") or {}).get("login", ""),
    }


def invoke_triage(payload: dict[str, Any]) -> None:
    """Asynchronously invoke the triage_dispatcher Lambda."""
    lambda_client().invoke(
        FunctionName=settings().triage_dispatcher_function,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
