"""POST /webhooks/github — HMAC-verified GitHub webhook receiver.

GitHub's webhook hits this route with no Cognito JWT (the ALB listener
rule lets it through unauthenticated). We verify the HMAC-SHA256
signature against the webhook secret stored in Secrets Manager, then
translate the GitHub event into a platform event on the EventBridge
bus. The state-router and event-projector handle the rest.

PR-derived events resolve the run/task by querying the runs table's
``gsi_pr`` index — the state-router writes ``pr_url`` onto the STATE
row when it opens the spec PR, and the projector writes ``pr_url``
onto the TASK row when it applies ``TASK.READY``. No PR-body marker
parsing.

Issue-derived events look up the run via the ``gsi1`` index
(``ISSUE#{url}``) when needed — the projector populated it on the
matching ``REQUEST.RECEIVED``.

Trigger convention:

* ``@aidlc-bot <natural language>`` — the only comment-driven trigger.
  On a PR / PR-review comment the body becomes feedback for the
  implementer (iteration). On a non-PR issue comment it triggers a
  fresh triage run.

Everything else is GitHub-native: merge a PR to approve, close a PR
to reject, close an issue to cancel its in-flight run, unassign the
bot from an issue to cancel.

Every accepted trigger posts a 👀 reaction on the source object — the
issue itself for assignment-driven triggers, the comment id for any
comment-driven trigger.

Event mapping:

* ``pull_request.closed`` (merged)        → ``SPEC.APPROVED`` / ``TASK.APPROVED``
* ``pull_request.closed`` (unmerged)      → ``SPEC.REJECTED`` / ``TASK.REJECTED``
* ``pull_request_review`` approved        → ``TASK.APPROVED``
* ``pull_request_review`` changes_requested → ``TASK.ITERATION_REQUESTED``
* ``pull_request_review_comment`` w/ ``@aidlc-bot``       → ``TASK.ITERATION_REQUESTED``
* ``issue_comment`` on a PR w/ ``@aidlc-bot``             → ``TASK.ITERATION_REQUESTED``
* ``workflow_run.completed`` failure                      → ``TASK.ITERATION_REQUESTED``
* ``issues`` opened/labeled/assigned (triage)             → ``REQUEST.RECEIVED``
* ``issues`` unassigned (bot) / closed                    → ``RUN.CANCEL_REQUESTED``
* ``issue_comment`` w/ ``@aidlc-bot`` / awaiting-response → ``REQUEST.RECEIVED``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from functools import cache
from typing import Any

import httpx
import structlog
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from fastapi import APIRouter, HTTPException, Request, status

from common import github_app as common_github
from common.event_emit import publish
from common.events import (
    EventEnvelope,
    RunCancelRequested,
    SpecApproved,
    SpecRejected,
    TaskApproved,
    TaskIterationRequested,
    TaskRejected,
)
from common.github_mentions import has_bot_mention
from common.ids import (
    CorrelationId,
    RunId,
    new_event_id,
)
from common.runs import IssueContext, start_run
from common.runtime import (
    CiFailureFeedback,
    FeedbackItem,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)
from common.slug import slug_from_repo
from dashboard.deps import ddb, secrets, settings

router = APIRouter()
logger = structlog.get_logger()

# Powertools' idempotency utility deduplicates issue-trigger
# REQUEST.RECEIVED emits keyed on the GitHub-supplied X-GitHub-Delivery
# header. Re-deliveries (network blip, GitHub retry) within the TTL
# return the cached run_id rather than minting a fresh run.
idempotency_persistence = DynamoDBPersistenceLayer(
    table_name=os.environ["AIDLC_IDEMPOTENCY_TABLE"],
    key_attr="idempotency_key",
    expiry_attr="expires_at",
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath="delivery_id",
    expires_after_seconds=86_400,
    raise_on_no_idempotency_key=True,
)

READY_LABEL = "aidlc:ready"
AWAITING_RESPONSE_LABEL = "aidlc:awaiting-response"
TERMINAL_LABELS = frozenset({"aidlc:in-progress", "aidlc:deferred", "aidlc:declined"})


# ---------------------------------------------------------------------------
# Entry + HMAC verification
# ---------------------------------------------------------------------------


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
    """Verify HMAC and translate the GitHub event into a platform event."""
    body = await request.body()
    verify_signature(body=body, signature_header=request.headers.get("x-hub-signature-256"))
    event_type = request.headers.get("x-github-event", "")
    delivery_id = request.headers.get("x-github-delivery", "")
    payload: dict[str, Any] = json.loads(body) if body else {}
    handler = HANDLERS.get(event_type)
    if handler is None:
        return {"ok": True, "ignored": True, "event": event_type}
    return handler(payload, delivery_id)


# ---------------------------------------------------------------------------
# DDB lookups
# ---------------------------------------------------------------------------


def lookup_pr(pr_url: str) -> dict[str, Any] | None:
    """Resolve a PR URL → STATE or TASK row via the ``gsi_pr`` index.

    Returns the raw DDB attribute map on hit, ``None`` on miss.
    Callers inspect ``sk`` to decide spec PR (``STATE``) vs task PR
    (``TASK#{task_id}``).
    """
    resp = ddb().query(
        TableName=settings().runs_table,
        IndexName="gsi_pr",
        KeyConditionExpression="pr_url = :p",
        ExpressionAttributeValues={":p": {"S": pr_url}},
        Limit=1,
    )
    items = resp.get("Items") or []
    return items[0] if items else None


def lookup_run_by_issue(issue_url: str) -> dict[str, Any] | None:
    """Resolve a GitHub issue URL → STATE row via ``gsi1``."""
    resp = ddb().query(
        TableName=settings().runs_table,
        IndexName="gsi1",
        KeyConditionExpression="gsi1pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"ISSUE#{issue_url}"}},
        Limit=1,
    )
    items = resp.get("Items") or []
    if not items:
        return None
    pk = items[0]["pk"]["S"]
    state = (
        ddb()
        .get_item(
            TableName=settings().runs_table,
            Key={"pk": {"S": pk}, "sk": {"S": "STATE"}},
        )
        .get("Item")
    )
    return state


def attr(item: dict[str, Any] | None, name: str) -> str:
    """Read an ``S`` attribute off a DDB item, defaulting to empty string."""
    if item is None:
        return ""
    return item.get(name, {}).get("S", "")


def run_id_of(item: dict[str, Any]) -> str:
    """Extract the run_id from a row's ``pk = RUN#{id}``."""
    return attr(item, "pk").removeprefix("RUN#")


def task_id_of(item: dict[str, Any]) -> str:
    """Extract the task_id from a TASK row's ``sk = TASK#{id}``."""
    return attr(item, "sk").removeprefix("TASK#")


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def emit(envelope: EventEnvelope[Any]) -> None:
    """Publish a single envelope onto the EventBridge bus."""
    publish(envelope)


def envelope_for(
    *,
    event_type: str,
    run_id: str,
    correlation_id: str,
    actor: str,
    payload: Any,
) -> EventEnvelope[Any]:
    """Build an envelope around the typed payload."""
    return EventEnvelope(
        event_id=new_event_id(),
        type=event_type,  # ty: ignore[invalid-argument-type]
        run_id=RunId(run_id),
        correlation_id=CorrelationId(correlation_id),
        actor_id=actor,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# pull_request → SPEC.APPROVED/REJECTED + TASK.APPROVED/REJECTED
# ---------------------------------------------------------------------------


def handle_pull_request(payload: dict[str, Any], _delivery_id: str) -> dict[str, Any]:
    """``pull_request.closed`` → approve/reject the corresponding gate."""
    if payload.get("action") != "closed":
        return {"ok": True, "ignored": True}
    pr = payload.get("pull_request") or {}
    pr_url = pr.get("html_url") or ""
    if not pr_url:
        return {"ok": True, "ignored": "no pr url"}
    row = lookup_pr(pr_url)
    if row is None:
        return {"ok": True, "ignored": "pr not tracked"}
    merged = bool(pr.get("merged"))
    reviewer = (pr.get("merged_by") or pr.get("user") or {}).get("login", "unknown")
    if not merged:
        reviewer = (payload.get("sender") or {}).get("login", "unknown")
    return emit_pr_close(row, pr_url=pr_url, merged=merged, reviewer=reviewer)


def emit_pr_close(
    row: dict[str, Any],
    *,
    pr_url: str,
    merged: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Branch on STATE row (spec PR) vs TASK row (task PR)."""
    sk = attr(row, "sk")
    run_id = run_id_of(row)
    correlation_id = attr(row, "correlation_id")
    project_slug = attr(row, "project_slug")
    spec_slug = attr(row, "spec_slug")
    spec_s3_prefix = attr(row, "spec_s3_prefix") or f"specs/{spec_slug}/"
    if sk == "STATE":
        if merged:
            payload = SpecApproved(
                project_slug=project_slug,
                spec_slug=spec_slug,
                spec_s3_prefix=spec_s3_prefix,
                reviewer=reviewer,
            )
            emit(
                envelope_for(
                    event_type="SPEC.APPROVED",
                    run_id=run_id,
                    correlation_id=correlation_id,
                    actor="webhook",
                    payload=payload,
                )
            )
            return {"ok": True, "decision": "spec_approved"}
        emit(
            envelope_for(
                event_type="SPEC.REJECTED",
                run_id=run_id,
                correlation_id=correlation_id,
                actor="webhook",
                payload=SpecRejected(
                    project_slug=project_slug,
                    spec_slug=spec_slug,
                    spec_s3_prefix=spec_s3_prefix,
                    reviewer=reviewer,
                    reason="PR closed without merge",
                ),
            )
        )
        return {"ok": True, "decision": "spec_rejected"}
    return emit_task_close(row, pr_url=pr_url, merged=merged, reviewer=reviewer)


def emit_task_close(
    row: dict[str, Any],
    *,
    pr_url: str,
    merged: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Emit TASK.APPROVED / TASK.REJECTED for a closed task PR."""
    run_id = run_id_of(row)
    correlation_id = attr(row, "correlation_id")
    project_slug = attr(row, "project_slug")
    spec_slug = attr(row, "spec_slug")
    task_id = task_id_of(row)
    common = {
        "project_slug": project_slug,
        "spec_slug": spec_slug,
        "task_id": task_id,
        "pr_url": pr_url,
        "reviewer": reviewer,
    }
    if merged:
        emit(
            envelope_for(
                event_type="TASK.APPROVED",
                run_id=run_id,
                correlation_id=correlation_id,
                actor="webhook",
                payload=TaskApproved(**common),
            )
        )
        return {"ok": True, "decision": "task_approved", "task_id": task_id}
    emit(
        envelope_for(
            event_type="TASK.REJECTED",
            run_id=run_id,
            correlation_id=correlation_id,
            actor="webhook",
            payload=TaskRejected(**common, reason="PR closed without merge"),
        )
    )
    return {"ok": True, "decision": "task_rejected", "task_id": task_id}


# ---------------------------------------------------------------------------
# pull_request_review → TASK.APPROVED / TASK.ITERATION_REQUESTED
# ---------------------------------------------------------------------------


def handle_pull_request_review(payload: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    """``pull_request_review.submitted`` → approve or request iteration."""
    if payload.get("action") != "submitted":
        return {"ok": True, "ignored": True}
    review = payload.get("review") or {}
    state = review.get("state")
    pr_url = (payload.get("pull_request") or {}).get("html_url") or ""
    if not pr_url:
        return {"ok": True, "ignored": "no pr url"}
    row = lookup_pr(pr_url)
    if row is None or attr(row, "sk") == "STATE":
        return {"ok": True, "ignored": "not a task PR"}
    reviewer = (review.get("user") or {}).get("login", "unknown")
    if state == "approved":
        return emit_task_close(row, pr_url=pr_url, merged=True, reviewer=reviewer)
    if state == "changes_requested":
        feedback = ReviewChangesRequestedFeedback(
            reviewer=reviewer,
            body=review.get("body") or "",
            review_id=int(review.get("id", 0)),
        )
        return emit_iteration(row, pr_url=pr_url, feedback=feedback, delivery_id=delivery_id)
    return {"ok": True, "ignored": f"review state {state}"}


# ---------------------------------------------------------------------------
# pull_request_review_comment → TASK.ITERATION_REQUESTED (on bot mention)
# ---------------------------------------------------------------------------


def handle_pull_request_review_comment(
    payload: dict[str, Any],
    delivery_id: str,
) -> dict[str, Any]:
    """``pull_request_review_comment.created`` w/ bot mention → iteration."""
    if payload.get("action") != "created":
        return {"ok": True, "ignored": True}
    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    if not has_bot_mention(body, settings().github_bot_login):
        return {"ok": True, "ignored": "no mention"}
    pr_url = (payload.get("pull_request") or {}).get("html_url") or ""
    row = lookup_pr(pr_url)
    if row is None or attr(row, "sk") == "STATE":
        return {"ok": True, "ignored": "not a task PR"}
    react_to_pr_review_comment(payload.get("repository") or {}, comment)
    feedback = ReviewCommentMentionFeedback(
        path=comment.get("path", ""),
        line=comment.get("line"),
        commit_id=comment.get("commit_id", ""),
        comment_id=int(comment.get("id", 0)),
        in_reply_to_id=comment.get("in_reply_to_id"),
        body=body,
        commenter=(comment.get("user") or {}).get("login", "unknown"),
    )
    return emit_iteration(row, pr_url=pr_url, feedback=feedback, delivery_id=delivery_id)


# ---------------------------------------------------------------------------
# issue_comment → multiple paths (PR comment vs issue comment)
# ---------------------------------------------------------------------------


def handle_issue_comment(payload: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    """``issue_comment.created`` — branches on PR vs issue + magic strings."""
    if payload.get("action") != "created":
        return {"ok": True, "ignored": True}
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    body = (comment.get("body") or "").strip()
    if issue.get("pull_request") is not None:
        return handle_pr_comment(payload, body, delivery_id=delivery_id)
    return handle_issue_only_comment(payload, body, delivery_id=delivery_id)


def handle_pr_comment(
    payload: dict[str, Any],
    body: str,
    *,
    delivery_id: str,
) -> dict[str, Any]:
    """Routes PR conversation comments by magic-string + bot mention."""
    issue = payload.get("issue") or {}
    issue_pr = issue.get("pull_request") or {}
    pr_url = issue_pr.get("html_url") or issue.get("html_url") or ""
    if not pr_url:
        return {"ok": True, "ignored": "no pr url"}
    row = lookup_pr(pr_url)
    if row is None:
        return {"ok": True, "ignored": "pr not tracked"}
    comment = payload.get("comment") or {}
    commenter = (comment.get("user") or {}).get("login", "unknown")
    return classify_pr_comment(
        row=row,
        body=body,
        comment=comment,
        repository=payload.get("repository") or {},
        pr_url=pr_url,
        commenter=commenter,
        delivery_id=delivery_id,
    )


def classify_pr_comment(
    *,
    row: dict[str, Any],
    body: str,
    comment: dict[str, Any],
    repository: dict[str, Any],
    pr_url: str,
    commenter: str,
    delivery_id: str,
) -> dict[str, Any]:
    """Pick the right event for a PR conversation comment + emit it.

    The only comment-driven trigger is ``@aidlc-bot``; merging or
    closing the PR handles approval / rejection natively. Posts a 👀
    reaction on the comment when the mention matches so users see
    immediate confirmation before the async pipeline runs.
    """
    if not has_bot_mention(body, settings().github_bot_login):
        return {"ok": True, "ignored": "no match"}
    if not attr(row, "sk").startswith("TASK#"):
        return {"ok": True, "ignored": "not a task PR"}
    react_to_issue_comment(repository, comment)
    feedback = IssueCommentMentionFeedback(
        comment_id=int(comment.get("id", 0)),
        body=body,
        commenter=commenter,
    )
    return emit_iteration(row, pr_url=pr_url, feedback=feedback, delivery_id=delivery_id)


def handle_issue_only_comment(
    payload: dict[str, Any],
    body: str,
    *,
    delivery_id: str = "",
) -> dict[str, Any]:
    """Comments on a non-PR issue.

    Recognised triggers (each posts a 👀 reaction on the comment):

    * ``@aidlc-bot`` (anywhere in body) → start a fresh triage run.
    * Any human comment when the issue carries ``aidlc:awaiting-response``
      → start a fresh triage run with the reply as additional context.

    Cancellation is GitHub-native: close the issue or unassign the bot.
    """
    issue = payload.get("issue") or {}
    label_names = [label.get("name", "") for label in issue.get("labels", [])]
    if set(label_names) & TERMINAL_LABELS:
        return {"ok": True, "ignored": "terminal label"}
    issue_url = issue.get("html_url") or ""
    repository = payload.get("repository") or {}
    comment = payload.get("comment") or {}
    if AWAITING_RESPONSE_LABEL in label_names and is_human_comment(comment):
        react_to_issue_comment(repository, comment)
        return emit_request_received(payload, source_issue_url=issue_url, delivery_id=delivery_id)
    if has_bot_mention(body, settings().github_bot_login):
        react_to_issue_comment(repository, comment)
        return emit_request_received(payload, source_issue_url=issue_url, delivery_id=delivery_id)
    return {"ok": True, "ignored": "no match"}


def is_human_comment(comment: dict[str, Any]) -> bool:
    """``True`` for human commenters; ``False`` for ``Bot`` users / aidlc bot."""
    user = comment.get("user") or {}
    if user.get("type") == "Bot":
        return False
    bot_login = settings().github_bot_login
    return not (bot_login and user.get("login") == bot_login)


# ---------------------------------------------------------------------------
# workflow_run.completed → TASK.ITERATION_REQUESTED
# ---------------------------------------------------------------------------


def handle_workflow_run(payload: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    """CI workflow finished with a non-success conclusion."""
    if payload.get("action") != "completed":
        return {"ok": True, "ignored": True}
    workflow_run = payload.get("workflow_run") or {}
    conclusion = workflow_run.get("conclusion")
    failing = {"failure", "timed_out", "cancelled", "action_required", "stale"}
    if conclusion not in failing:
        return {"ok": True, "ignored": f"conclusion={conclusion}"}
    pull_requests = workflow_run.get("pull_requests") or []
    if not pull_requests:
        return {"ok": True, "ignored": "no pr"}
    pr_url = pull_requests[0].get("html_url") or ""
    if not pr_url:
        repo_html = (payload.get("repository") or {}).get("html_url", "")
        pr_url = f"{repo_html}/pull/{pull_requests[0].get('number', 0)}"
    row = lookup_pr(pr_url)
    if row is None or attr(row, "sk") == "STATE":
        return {"ok": True, "ignored": "not a task PR"}
    feedback = CiFailureFeedback(
        workflow_name=workflow_run.get("name", "(unknown)"),
        conclusion=conclusion,
        head_sha=workflow_run.get("head_sha", ""),
        html_url=workflow_run.get("html_url", ""),
    )
    return emit_iteration(row, pr_url=pr_url, feedback=feedback, delivery_id=delivery_id)


# ---------------------------------------------------------------------------
# issues → REQUEST.RECEIVED (triage triggers) / RUN.CANCEL_REQUESTED (unassign)
# ---------------------------------------------------------------------------


def handle_issues(payload: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    """Branch on action: triage triggers vs cancel."""
    action = payload.get("action")
    if action == "unassigned":
        return handle_issue_unassigned(payload)
    if action == "closed":
        return handle_issue_closed(payload)
    if action in {"opened", "labeled", "assigned"} and issues_action_is_trigger(action, payload):
        issue = payload.get("issue") or {}
        issue_url = issue.get("html_url") or ""
        react_to_issue(payload.get("repository") or {}, issue)
        return emit_request_received(payload, source_issue_url=issue_url, delivery_id=delivery_id)
    return {"ok": True, "ignored": True}


def handle_issue_unassigned(payload: dict[str, Any]) -> dict[str, Any]:
    """Bot unassigned from an issue → cancel the in-flight run."""
    bot_login = settings().github_bot_login
    if not bot_login:
        return {"ok": True, "ignored": "no bot login configured"}
    if (payload.get("assignee") or {}).get("login", "") != bot_login:
        return {"ok": True, "ignored": "non-bot unassignment"}
    issue_url = (payload.get("issue") or {}).get("html_url", "")
    state = lookup_run_by_issue(issue_url) if issue_url else None
    if state is None:
        return {"ok": True, "ignored": "no run for issue"}
    sender = (payload.get("sender") or {}).get("login", "unknown")
    return emit_run_cancel(
        state,
        requestor=sender,
        source="issue_unassigned",
        reason=f"bot unassigned from {issue_url} by {sender}",
    )


def handle_issue_closed(payload: dict[str, Any]) -> dict[str, Any]:
    """Issue closed → cancel the in-flight run if there is one.

    Replaces the previous ``/aidlc cancel`` comment trigger. Closing
    the issue is the GitHub-native "I'm done with this" signal; the
    in-flight run terminates so it doesn't keep firing agents.
    """
    issue_url = (payload.get("issue") or {}).get("html_url", "")
    state = lookup_run_by_issue(issue_url) if issue_url else None
    if state is None:
        return {"ok": True, "ignored": "no run for issue"}
    sender = (payload.get("sender") or {}).get("login", "unknown")
    return emit_run_cancel(
        state,
        requestor=sender,
        source="issue_closed",
        reason=f"issue {issue_url} closed by {sender}",
    )


def issues_action_is_trigger(action: str | None, payload: dict[str, Any]) -> bool:
    """``True`` if this ``issues`` action should kick off a triage run."""
    issue = payload.get("issue", {})
    label_names = {label.get("name") for label in issue.get("labels", []) if label.get("name")}
    if label_names & TERMINAL_LABELS:
        return False
    if action == "opened":
        return READY_LABEL in label_names
    if action == "labeled":
        return payload.get("label", {}).get("name") == READY_LABEL
    if action == "assigned":
        bot_login = settings().github_bot_login
        return bool(bot_login and (payload.get("assignee") or {}).get("login", "") == bot_login)
    return False


# ---------------------------------------------------------------------------
# Helpers — shared event emission
# ---------------------------------------------------------------------------


def emit_iteration(
    row: dict[str, Any],
    *,
    pr_url: str,
    feedback: FeedbackItem,
    delivery_id: str,
) -> dict[str, Any]:
    """Publish one TASK.ITERATION_REQUESTED for a task row + feedback."""
    if not delivery_id:
        return {"ok": True, "ignored": "missing delivery_id"}
    run_id = run_id_of(row)
    correlation_id = attr(row, "correlation_id")
    payload = TaskIterationRequested(
        project_slug=attr(row, "project_slug"),
        spec_slug=attr(row, "spec_slug"),
        task_id=task_id_of(row),
        pr_url=pr_url,
        delivery_id=delivery_id,
        feedback=feedback,
    )
    emit(
        envelope_for(
            event_type="TASK.ITERATION_REQUESTED",
            run_id=run_id,
            correlation_id=correlation_id,
            actor="webhook",
            payload=payload,
        )
    )
    return {"ok": True, "iteration": feedback.kind, "task_id": payload.task_id}


def emit_run_cancel(
    state: dict[str, Any],
    *,
    requestor: str,
    source: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Publish a RUN.CANCEL_REQUESTED for the given run STATE row."""
    run_id = run_id_of(state)
    correlation_id = attr(state, "correlation_id")
    payload = RunCancelRequested(
        project_slug=attr(state, "project_slug"),
        requestor=requestor,
        source=source,  # ty: ignore[invalid-argument-type]
        reason=reason,
    )
    emit(
        envelope_for(
            event_type="RUN.CANCEL_REQUESTED",
            run_id=run_id,
            correlation_id=correlation_id,
            actor="webhook",
            payload=payload,
        )
    )
    return {"ok": True, "cancel_run": run_id}


@idempotent_function(
    data_keyword_argument="trigger",
    config=idempotency_config,
    persistence_store=idempotency_persistence,
)
def trigger_request_received(*, trigger: dict[str, Any]) -> dict[str, Any]:
    """Idempotent shell that mints a run for an issue-driven trigger.

    Keyed on ``trigger.delivery_id`` (the GitHub-supplied
    ``X-GitHub-Delivery`` header). Re-deliveries within the TTL return
    the cached response without minting a duplicate ``run_id``.
    """
    payload = trigger["payload"]
    source_issue_url = trigger["source_issue_url"]
    issue_payload = payload.get("issue") or {}
    comment_payload = payload.get("comment") or {}
    repo = (payload.get("repository") or {}).get("full_name", "")
    requestor = (issue_payload.get("user") or {}).get("login", "github")
    intent = issue_payload.get("title") or "(no title)"
    issue_ctx = build_issue_context(issue_payload, source_issue_url, comment_payload)
    run_id, _ = start_run(
        project_slug=slug_from_repo(repo) if repo else "unknown",
        intent=intent,
        requestor=requestor,
        target_repo=repo or None,
        issue=issue_ctx,
        actor_id="webhook",
    )
    return {"ok": True, "triage": source_issue_url, "run_id": str(run_id)}


def emit_request_received(
    payload: dict[str, Any],
    *,
    source_issue_url: str,
    delivery_id: str = "",
) -> dict[str, Any]:
    """Mint a fresh run for an issue-driven trigger.

    Delegates to the idempotent inner function keyed on the
    ``X-GitHub-Delivery`` header so retries from GitHub don't mint
    duplicate runs. When ``delivery_id`` is missing (legacy callers,
    tests) we skip the idempotency layer.
    """
    if not delivery_id:
        return trigger_request_received.__wrapped__(
            trigger={
                "delivery_id": "",
                "payload": payload,
                "source_issue_url": source_issue_url,
            },
        )
    return trigger_request_received(
        trigger={
            "delivery_id": delivery_id,
            "payload": payload,
            "source_issue_url": source_issue_url,
        },
    )


def build_issue_context(
    issue_payload: dict[str, Any],
    source_issue_url: str,
    comment_payload: dict[str, Any] | None = None,
) -> IssueContext | None:
    """Pack the GitHub ``issue`` payload into an :class:`IssueContext`.

    Returns ``None`` for triggers without an issue number — caller treats
    this as a programmatic run rather than an issue-driven one.

    When ``comment_payload`` is non-empty (``issue_comment`` triggers),
    the comment body and commenter login are forwarded so the downstream
    agent can interpret a free-form follow-up reply alongside the
    original issue body.
    """
    if not source_issue_url:
        return None
    raw_number = issue_payload.get("number")
    if not isinstance(raw_number, int):
        return None
    labels = tuple(
        label.get("name", "")
        for label in (issue_payload.get("labels") or [])
        if isinstance(label, dict) and label.get("name")
    )
    comment = comment_payload or {}
    triggering_body = (comment.get("body") or "").strip()
    triggering_commenter = (comment.get("user") or {}).get("login") or ""
    return IssueContext(
        issue_url=source_issue_url,
        issue_number=raw_number,
        issue_title=issue_payload.get("title") or "(no title)",
        issue_body=issue_payload.get("body") or "",
        issue_labels=labels,
        triggering_comment_body=triggering_body,
        triggering_commenter=triggering_commenter,
    )


# ---------------------------------------------------------------------------
# React-eyes — gives users immediate confirmation on every accepted trigger
# ---------------------------------------------------------------------------


def react_eyes(*, repo: str, reactions_url: str) -> None:
    """POST a 👀 reaction to ``reactions_url``. Best-effort.

    ``reactions_url`` is the full GitHub reactions endpoint — issue,
    issue-comment, or PR-review-comment. ``repo`` is needed to mint the
    App installation token. Failures are logged and swallowed: a
    missing reaction is bad UX but never a reason to fail the webhook.
    """
    try:
        token = common_github.installation_token_for_repo(repo)
        response = httpx.post(
            reactions_url,
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
        logger.warning("react_eyes failed", error=str(exc), url=reactions_url)


def issue_reactions_url(repo: str, issue_number: int) -> str:
    """URL for posting a reaction on a GitHub issue."""
    return f"{common_github.GITHUB_API}/repos/{repo}/issues/{issue_number}/reactions"


def issue_comment_reactions_url(repo: str, comment_id: int) -> str:
    """URL for posting a reaction on an issue or PR-conversation comment."""
    return f"{common_github.GITHUB_API}/repos/{repo}/issues/comments/{comment_id}/reactions"


def pr_review_comment_reactions_url(repo: str, comment_id: int) -> str:
    """URL for posting a reaction on a PR-review (inline diff) comment."""
    return f"{common_github.GITHUB_API}/repos/{repo}/pulls/comments/{comment_id}/reactions"


def react_to_issue(repository: dict[str, Any], issue: dict[str, Any]) -> None:
    """Eyes-react on the issue itself (assignment-driven triggers)."""
    repo = repository.get("full_name")
    issue_number = issue.get("number")
    if not (repo and isinstance(issue_number, int)):
        return
    react_eyes(repo=repo, reactions_url=issue_reactions_url(repo, issue_number))


def react_to_issue_comment(repository: dict[str, Any], comment: dict[str, Any]) -> None:
    """Eyes-react on an issue-conversation or PR-conversation comment."""
    repo = repository.get("full_name")
    comment_id = comment.get("id")
    if not (repo and isinstance(comment_id, int)):
        return
    react_eyes(repo=repo, reactions_url=issue_comment_reactions_url(repo, comment_id))


def react_to_pr_review_comment(repository: dict[str, Any], comment: dict[str, Any]) -> None:
    """Eyes-react on a PR-review (inline diff) comment."""
    repo = repository.get("full_name")
    comment_id = comment.get("id")
    if not (repo and isinstance(comment_id, int)):
        return
    react_eyes(repo=repo, reactions_url=pr_review_comment_reactions_url(repo, comment_id))


# ---------------------------------------------------------------------------
# Dispatch table — one entry per GitHub event type
# ---------------------------------------------------------------------------


HANDLERS: dict[str, Any] = {
    "pull_request": handle_pull_request,
    "pull_request_review": handle_pull_request_review,
    "pull_request_review_comment": handle_pull_request_review_comment,
    "issue_comment": handle_issue_comment,
    "issues": handle_issues,
    "workflow_run": handle_workflow_run,
}
