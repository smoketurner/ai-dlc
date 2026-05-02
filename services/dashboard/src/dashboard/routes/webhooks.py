"""POST /webhooks/github — HMAC-verified GitHub webhook receiver.

GitHub's webhook hits this route with no Cognito JWT (the ALB listener rule
lets it through unauthenticated). We verify the HMAC-SHA256 signature against
the webhook secret stored in Secrets Manager, parse the PR review event, and
forward an approve/reject decision to the ``hitl_handler`` Lambda's
``DECIDE`` op.

Event mapping:
  * pull_request_review.submitted with state=approved   → decision=approve
  * pull_request_review.submitted with state=changes_requested → decision=reject
  * issue_comment.created with body containing magic strings:
        ``/aidlc approve``         → approve
        ``/aidlc reject <reason>`` → reject
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
    """Verify HMAC, extract decision, forward to hitl_handler."""
    body = await request.body()
    verify_signature(body=body, signature_header=request.headers.get("x-hub-signature-256"))
    event_type = request.headers.get("x-github-event", "")
    payload: dict[str, Any] = json.loads(body) if body else {}
    decision = parse_decision(event_type, payload)
    if decision is None:
        return {"ok": True, "ignored": True, "event": event_type}
    invoke_hitl_decide(decision)
    return {"ok": True, "decision": decision["decision"], "gate_ref": decision["gate_ref"]}


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
