"""POST /webhooks/github — HMAC-verified GitHub webhook receiver.

GitHub's webhook hits this route with no Cognito JWT (the ALB listener rule
lets it through unauthenticated). We verify the HMAC-SHA256 signature against
the webhook secret stored in Secrets Manager, then dispatch on event type:

* HITL gates (forwarded to the ``hitl_handler`` Lambda's ``DECIDE`` op):
    * pull_request_review.submitted state=approved          → approve
    * pull_request_review.submitted state=changes_requested → reject
    * issue_comment.created on a PR with body containing
      ``/aidlc approve`` or ``/aidlc reject <reason>``      → approve / reject
    * pull_request.closed merged=true                       → approve
    * pull_request.closed merged=false                      → reject

* Cancellation (forwarded to ``hitl_handler`` ``CANCEL_RUN`` op):
    * issues.unassigned when the configured bot login is the unassignee.
      We resolve the issue → run via the runs-table ``gsi1`` (``ISSUE#``)
      lookup the projector populated, then fail every PENDING gate.

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

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, status

from common import github_app as common_github
from common.github_mentions import has_bot_mention
from dashboard.deps import ddb, lambda_client, secrets, settings

router = APIRouter()
logger = structlog.get_logger()

APPROVE_RE = re.compile(r"/aidlc\s+approve\b", re.IGNORECASE)
REJECT_RE = re.compile(r"/aidlc\s+reject\s*(.*)", re.IGNORECASE)
GO_RE = re.compile(r"/aidlc\s+go\b", re.IGNORECASE)

READY_LABEL = "aidlc:ready"
AWAITING_RESPONSE_LABEL = "aidlc:awaiting-response"
CANCELLED_LABEL = "aidlc:cancelled"
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
    """Verify HMAC and route to hitl_handler (HITL gate / cancel) or triage_dispatcher."""
    body = await request.body()
    verify_signature(body=body, signature_header=request.headers.get("x-hub-signature-256"))
    event_type = request.headers.get("x-github-event", "")
    payload: dict[str, Any] = json.loads(body) if body else {}

    decision = parse_decision(event_type, payload)
    if decision is not None:
        invoke_hitl(decision)
        return {"ok": True, "decision": decision["decision"], "gate_ref": decision["gate_ref"]}

    delivery_id = request.headers.get("x-github-delivery", "")
    iteration = parse_iteration(event_type, payload, delivery_id=delivery_id)
    if iteration is not None:
        invoke_reactor(iteration)
        return {
            "ok": True,
            "iteration": iteration["trigger_kind"],
            "task_id": iteration["task_id"],
        }

    cancel = parse_cancellation(event_type, payload)
    if cancel is not None:
        invoke_hitl(cancel)
        return {"ok": True, "cancel_run": cancel["run_id"]}

    triage = parse_triage(event_type, payload)
    if triage is not None:
        react_eyes(triage)
        invoke_triage(triage)
        return {"ok": True, "triage": triage["issue_url"]}

    return {"ok": True, "ignored": True, "event": event_type}


def parse_decision(event_type: str, payload: dict[str, Any]) -> dict[str, str] | None:
    """Return a DECIDE payload, or ``None`` if this webhook doesn't approve a gate."""
    if event_type == "pull_request_review":
        return decision_from_review(payload)
    if event_type == "issue_comment":
        return decision_from_comment(payload)
    if event_type == "pull_request":
        return decision_from_pr_close(payload)
    return None


def decision_from_review(payload: dict[str, Any]) -> dict[str, str] | None:
    """Parse a ``pull_request_review.submitted state=approved`` payload.

    ``state=changes_requested`` reviews used to short-circuit the run as
    a HITL ``reject`` — they now route to :func:`parse_iteration` so the
    implementer can address the requested changes with a fix commit.
    """
    review = payload.get("review", {})
    if review.get("state") != "approved":
        return None
    pr = payload.get("pull_request", {})
    run_id, gate_ref = parse_run_meta(pr.get("body", "") or "")
    if run_id is None or gate_ref is None:
        return None
    return {
        "op": "DECIDE",
        "run_id": run_id,
        "gate_ref": gate_ref,
        "decision": "approve",
        "reviewer": review.get("user", {}).get("login", "unknown"),
        "reason": review.get("body") or "",
    }


def decision_from_pr_close(payload: dict[str, Any]) -> dict[str, str] | None:
    """Resolve the task gate when a PR carrying our run_id markers closes.

    Lets humans merge or close a PR via the GitHub UI without leaving an
    explicit review or ``/aidlc approve`` comment — without this, the
    Step Functions task gate would wait for a review that never comes.

    * ``merged: true``  → approve, attribute to the merger.
    * ``merged: false`` → reject, reason "PR closed without merge".
    """
    if payload.get("action") != "closed":
        return None
    pr = payload.get("pull_request", {})
    run_id, gate_ref = parse_run_meta(pr.get("body", "") or "")
    if run_id is None or gate_ref is None:
        return None
    if pr.get("merged"):
        merger = (pr.get("merged_by") or pr.get("user") or {}).get("login", "unknown")
        return {
            "op": "DECIDE",
            "run_id": run_id,
            "gate_ref": gate_ref,
            "decision": "approve",
            "reviewer": merger,
            "reason": "PR merged",
        }
    closer = (payload.get("sender") or {}).get("login", "unknown")
    return {
        "op": "DECIDE",
        "run_id": run_id,
        "gate_ref": gate_ref,
        "decision": "reject",
        "reviewer": closer,
        "reason": "PR closed without merge",
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
PROJECT_META_RE = re.compile(r"_project:\s*([\w-]+)_")
SPEC_META_RE = re.compile(r"_spec:\s*`docs/specs/([\w-]+)/`_")
CORRELATION_META_RE = re.compile(r"_correlation_id:\s*([\w-]+)_")
TASK_GATE_REF_RE = re.compile(r"^task:(T-\d+)$")


def parse_run_meta(body: str) -> tuple[str | None, str | None]:
    """Pull ``run_id`` and ``gate_ref`` out of the PR/issue body footer."""
    match = RUN_META_RE.search(body)
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def parse_iteration_meta(body: str) -> dict[str, str | None]:
    """Extract every metadata field the iteration_reactor needs from a PR body.

    Returns a flat dict (project_slug, spec_slug, correlation_id) — each
    value is ``None`` when the corresponding marker is missing. The PR
    body footer is written by ``implementer.client.pr_body_footer``;
    if the implementer was rebuilt before this rolled out, some markers
    will be absent and the iteration trigger silently no-ops.
    """
    project_match = PROJECT_META_RE.search(body)
    spec_match = SPEC_META_RE.search(body)
    correlation_match = CORRELATION_META_RE.search(body)
    return {
        "project_slug": project_match.group(1) if project_match else None,
        "spec_slug": spec_match.group(1) if spec_match else None,
        "correlation_id": correlation_match.group(1) if correlation_match else None,
    }


def task_id_from_gate_ref(gate_ref: str) -> str | None:
    """Extract the task_id (``T-NNN``) from a ``task:T-NNN`` gate ref."""
    match = TASK_GATE_REF_RE.match(gate_ref)
    return match.group(1) if match else None


def invoke_hitl(payload: dict[str, Any]) -> None:
    """Synchronously invoke the hitl_handler Lambda with the given op payload."""
    lambda_client().invoke(
        FunctionName=settings().hitl_handler_function,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def parse_cancellation(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a CANCEL_RUN payload when the bot was unassigned from an issue.

    Looks up the in-flight run for the issue via the runs table's ``gsi1``
    index (``ISSUE#{url}`` → ``RUN#{run_id}``). Returns ``None`` if no
    bot login is configured, the unassignee isn't the bot, or no run is
    found for the issue.
    """
    if event_type != "issues" or payload.get("action") != "unassigned":
        return None
    bot_login = settings().github_bot_login
    if not bot_login:
        return None
    if (payload.get("assignee") or {}).get("login", "") != bot_login:
        return None
    issue_url = (payload.get("issue") or {}).get("html_url")
    if not issue_url:
        return None
    run_id = lookup_run_by_issue(issue_url)
    if run_id is None:
        return None
    sender = (payload.get("sender") or {}).get("login", "unknown")
    return {
        "op": "CANCEL_RUN",
        "run_id": run_id,
        "reviewer": sender,
        "reason": f"bot unassigned from {issue_url} by {sender}",
    }


def lookup_run_by_issue(issue_url: str) -> str | None:
    """Query the runs table's ``gsi1`` for the run associated with this issue."""
    resp = ddb().query(
        TableName=settings().runs_table,
        IndexName="gsi1",
        KeyConditionExpression="gsi1pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"ISSUE#{issue_url}"}},
        ProjectionExpression="pk",
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    return items[0]["pk"]["S"].removeprefix("RUN#")


def parse_iteration(
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str,
) -> dict[str, Any] | None:
    """Return an iteration-trigger payload, or ``None`` if no trigger fires.

    Four event paths land here:

    * ``pull_request_review.submitted`` with ``state=changes_requested``
      (formerly a HITL ``reject``)
    * ``pull_request_review_comment.created`` with a bot @-mention
    * ``issue_comment.created`` on a PR with a bot @-mention (and not
      a ``/aidlc approve|reject`` magic-string — those still go to HITL)
    * ``workflow_run.completed`` with a non-success conclusion

    All four require the PR body footer's metadata; PRs not opened by
    the implementer (no footer) silently no-op.
    """
    if not delivery_id:
        return None
    if event_type == "pull_request_review":
        return iteration_from_review(payload, delivery_id=delivery_id)
    if event_type == "pull_request_review_comment":
        return iteration_from_review_comment(payload, delivery_id=delivery_id)
    if event_type == "issue_comment":
        return iteration_from_pr_comment(payload, delivery_id=delivery_id)
    if event_type == "workflow_run":
        return iteration_from_workflow_run(payload, delivery_id=delivery_id)
    return None


def iteration_from_review(payload: dict[str, Any], *, delivery_id: str) -> dict[str, Any] | None:
    """A reviewer asked for changes — re-invoke the implementer with the review body."""
    review = payload.get("review", {})
    if review.get("state") != "changes_requested":
        return None
    pr = payload.get("pull_request", {})
    base = build_iteration_base(pr, payload, delivery_id=delivery_id)
    if base is None:
        return None
    return {
        **base,
        "trigger_kind": "review_changes_requested",
        "trigger_payload": {
            "reviewer": review.get("user", {}).get("login", "unknown"),
            "body": review.get("body") or "",
            "review_id": int(review.get("id", 0)),
        },
    }


def iteration_from_review_comment(
    payload: dict[str, Any],
    *,
    delivery_id: str,
) -> dict[str, Any] | None:
    """An inline review comment that @-mentions the bot."""
    if payload.get("action") != "created":
        return None
    comment = payload.get("comment", {})
    if not has_bot_mention(comment.get("body"), settings().github_bot_login):
        return None
    pr = payload.get("pull_request", {})
    base = build_iteration_base(pr, payload, delivery_id=delivery_id)
    if base is None:
        return None
    return {
        **base,
        "trigger_kind": "review_comment_mention",
        "trigger_payload": {
            "path": comment.get("path", ""),
            "line": comment.get("line"),
            "commit_id": comment.get("commit_id", base["head_sha"]),
            "comment_id": int(comment.get("id", 0)),
            "in_reply_to_id": comment.get("in_reply_to_id"),
            "body": comment.get("body") or "",
            "commenter": comment.get("user", {}).get("login", "unknown"),
        },
    }


def iteration_from_pr_comment(
    payload: dict[str, Any],
    *,
    delivery_id: str,
) -> dict[str, Any] | None:
    """A PR-conversation comment that @-mentions the bot.

    Skipped when the comment also matches ``/aidlc approve|reject`` —
    those still flow through ``decision_from_comment`` to HITL so a
    human's explicit gate decision wins over an iteration request.
    """
    if payload.get("action") != "created":
        return None
    if payload.get("issue", {}).get("pull_request") is None:
        return None
    comment = payload.get("comment", {})
    body = comment.get("body") or ""
    if APPROVE_RE.search(body) or REJECT_RE.search(body):
        return None
    if not has_bot_mention(body, settings().github_bot_login):
        return None
    issue = payload.get("issue", {})
    issue_pr = issue.get("pull_request") or {}
    synthetic_pr = {
        "body": issue.get("body") or "",
        "html_url": issue_pr.get("html_url") or issue.get("html_url", ""),
        "number": issue.get("number"),
    }
    base = build_iteration_base(synthetic_pr, payload, delivery_id=delivery_id)
    if base is None:
        return None
    return {
        **base,
        "trigger_kind": "issue_comment_mention",
        "trigger_payload": {
            "comment_id": int(comment.get("id", 0)),
            "body": body,
            "commenter": comment.get("user", {}).get("login", "unknown"),
        },
    }


def iteration_from_workflow_run(
    payload: dict[str, Any],
    *,
    delivery_id: str,
) -> dict[str, Any] | None:
    """A CI workflow run finished with a failing conclusion."""
    if payload.get("action") != "completed":
        return None
    workflow_run = payload.get("workflow_run", {})
    conclusion = workflow_run.get("conclusion")
    failing = {"failure", "timed_out", "cancelled", "action_required", "stale"}
    if conclusion not in failing:
        return None
    pull_requests = workflow_run.get("pull_requests") or []
    if not pull_requests:
        return None
    # workflow_run.pull_requests doesn't carry the body — fetch from the
    # event's pull_request when present, else accept the limited subset.
    pr = pull_requests[0]
    pr_body = pr.get("body") or workflow_run.get("head_commit", {}).get("message", "") or ""
    pr_url = (
        pr.get("html_url")
        or f"{(payload.get('repository') or {}).get('html_url', '')}/pull/{pr.get('number', 0)}"
    )
    pr_for_meta = {"body": pr_body, "html_url": pr_url, "number": pr.get("number", 0)}
    base = build_iteration_base(
        pr_for_meta,
        payload,
        delivery_id=delivery_id,
        head_sha_override=workflow_run.get("head_sha"),
    )
    if base is None:
        return None
    return {
        **base,
        "trigger_kind": "ci_failure",
        "trigger_payload": {
            "workflow_name": workflow_run.get("name", "(unknown)"),
            "conclusion": conclusion,
            "html_url": workflow_run.get("html_url", ""),
        },
    }


def build_iteration_base(
    pr: dict[str, Any],
    payload: dict[str, Any],
    *,
    delivery_id: str,
    head_sha_override: str | None = None,
) -> dict[str, Any] | None:
    """Build the common fields every iteration trigger needs.

    Returns ``None`` when the PR body lacks the implementer-written
    metadata footer or when the gate_ref isn't a task gate (e.g. the
    spec gate has no task to iterate on).
    """
    body = pr.get("body") or ""
    run_id, gate_ref = parse_run_meta(body)
    if run_id is None or gate_ref is None:
        return None
    task_id = task_id_from_gate_ref(gate_ref)
    if task_id is None:
        return None
    extras = parse_iteration_meta(body)
    if not (extras["project_slug"] and extras["spec_slug"] and extras["correlation_id"]):
        return None
    pr_url = pr.get("html_url")
    pr_number = pr.get("number")
    # head_sha is empty for issue_comment webhooks (GitHub doesn't include
    # it in the comment payload). The reactor + implementer recompute it
    # from the branch HEAD on checkout, so an empty value here is OK.
    head_sha = head_sha_override or (pr.get("head") or {}).get("sha") or ""
    target_repo = (payload.get("repository") or {}).get("full_name")
    if not (pr_url and isinstance(pr_number, int) and target_repo):
        return None
    return {
        "run_id": run_id,
        "task_id": task_id,
        "correlation_id": extras["correlation_id"],
        "project_slug": extras["project_slug"],
        "spec_slug": extras["spec_slug"],
        "spec_s3_prefix": f"specs/{extras['spec_slug']}/",
        "target_repo": target_repo,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "delivery_id": delivery_id,
    }


def invoke_reactor(payload: dict[str, Any]) -> None:
    """Synchronously invoke the iteration_reactor Lambda."""
    lambda_client().invoke(
        FunctionName=settings().iteration_reactor_function,
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


def react_eyes(triage: dict[str, Any]) -> None:
    """Post a 👀 reaction on the source issue.

    Best-effort: a failure is logged but does not block triage. Gives the
    user immediate confirmation that the bot picked up the assignment,
    before the triage agent and SDLC pipeline run. Uses the App's
    installation token (no user-OBO needed — this is a system reaction).
    """
    repo = triage["repo"]
    issue_number = triage["issue_number"]
    try:
        token = common_github.installation_token_for_repo(repo)
        response = httpx.post(
            f"{common_github.GITHUB_API}/repos/{repo}/issues/{issue_number}/reactions",
            headers={
                "Accept": common_github.ACCEPT_HEADER,
                "Authorization": f"Bearer {token}",
                "User-Agent": common_github.USER_AGENT,
                "X-GitHub-Api-Version": common_github.API_VERSION,
            },
            json={"content": "eyes"},
            timeout=common_github.HTTP_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning(
            "react_eyes failed",
            error=str(exc),
            repo=repo,
            issue_number=issue_number,
        )
