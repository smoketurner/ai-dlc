"""Per-agent AgentCore payload builders.

Each agent has its own pydantic input contract in :mod:`common.runtime`.
These builders pack the right fields out of the run's event history
into a plain dict suitable for the AgentCore Runtime ``payload``
parameter. The pydantic schema lives on the agent side, not here —
this module produces dicts that match the contract by construction.

Single source of truth for "what does the implementer need from the
run?" — every change to the input shape is a one-line edit here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from common.github_mentions import strip_bot_mention
from state_router.config import github_bot_login
from state_router.extract import (
    EnvelopeLike,
    correlation_id,
    intent,
    issue_payload,
    latest_triggering_comment,
    plan_s3_key,
    pr_url,
    project_slug,
    requestor_sub,
    revision_feedback,
    run_id,
    source_issue_url,
    target_repo,
)


def triage_payload(events: Sequence[EnvelopeLike]) -> dict[str, Any]:
    """Build the ``TriageInput`` dict."""
    issue = issue_payload(events)
    comment_body, _ = latest_triggering_comment(events)
    return {
        "project_slug": project_slug(events),
        "target_repo": target_repo(events),
        "issue_url": issue.get("issue_url", ""),
        "issue_number": issue.get("issue_number"),
        "issue_title": issue.get("issue_title", ""),
        "issue_body": issue.get("issue_body", ""),
        "issue_labels": issue.get("issue_labels", []),
        "triggering_comment_body": strip_bot_mention(comment_body, github_bot_login()),
        "run_id": run_id(events),
        "correlation_id": correlation_id(events),
        "actor_id": "state_router",
        "requestor_sub": requestor_sub(events),
    }


def architect_payload(events: Sequence[EnvelopeLike]) -> dict[str, Any]:
    """Build the ``ArchitectInput`` dict."""
    issue = issue_payload(events)
    comment_body, _ = latest_triggering_comment(events)
    return {
        "project_slug": project_slug(events),
        "intent": intent(events) or issue.get("issue_title", ""),
        "triggering_comment_body": strip_bot_mention(comment_body, github_bot_login()),
        "run_id": run_id(events),
        "correlation_id": correlation_id(events),
        "actor_id": "state_router",
        "requestor_sub": requestor_sub(events),
        "target_repo": target_repo(events),
        "source_issue_url": source_issue_url(events),
        "source_issue_title": issue.get("issue_title"),
        "source_issue_body": issue.get("issue_body"),
    }


def implementer_payload(
    events: Sequence[EnvelopeLike],
    *,
    mode: str,
    revision_number: int,
) -> dict[str, Any]:
    """Build the ``ImplementerInput`` dict for the requested mode."""
    issue = issue_payload(events)
    payload: dict[str, Any] = {
        "project_slug": project_slug(events),
        "run_id": run_id(events),
        "correlation_id": correlation_id(events),
        "actor_id": "state_router",
        "mode": mode,
        "plan_s3_key": plan_s3_key(events) or None,
        "revision_number": revision_number,
        "requestor_sub": requestor_sub(events),
        "target_repo": target_repo(events),
        "source_issue_url": source_issue_url(events),
        "source_issue_title": issue.get("issue_title"),
        "intent": intent(events) or issue.get("issue_title"),
    }
    pr = pr_url(events)
    if pr:
        payload["pr_url"] = pr
    if mode == "revision":
        feedback = revision_feedback(events)
        if feedback:
            payload["revision_feedback"] = list(feedback)
    return payload


def validator_payload(
    events: Sequence[EnvelopeLike],
    *,
    revision_number: int,
    include_issue_context: bool = False,
) -> dict[str, Any]:
    """Build the common ``ReviewerInput`` / ``TesterInput`` / ``CodeCriticInput`` dict.

    ``include_issue_context`` flips on the code-critic-specific fields
    (``source_issue_*``); reviewer and tester ignore them.
    """
    payload: dict[str, Any] = {
        "project_slug": project_slug(events),
        "plan_s3_key": plan_s3_key(events),
        "pr_url": pr_url(events),
        "run_id": run_id(events),
        "correlation_id": correlation_id(events),
        "actor_id": "state_router",
        "requestor_sub": requestor_sub(events),
        "revision_number": revision_number,
    }
    if include_issue_context:
        issue = issue_payload(events)
        payload["source_issue_url"] = source_issue_url(events)
        payload["source_issue_title"] = issue.get("issue_title")
        payload["source_issue_body"] = issue.get("issue_body")
    return payload


def proposer_payload(events: Sequence[EnvelopeLike]) -> dict[str, Any]:
    """Build the ``ProposerInput`` dict (research-path runs)."""
    issue = issue_payload(events)
    comment_body, commenter = latest_triggering_comment(events)
    body = issue.get("issue_body", "")
    title = intent(events) or issue.get("issue_title", "")
    combined = f"{title}\n\n{body}".strip() if body else title
    return {
        "project_slug": project_slug(events),
        "target_repo": target_repo(events),
        "trigger_reason": "research",
        "intent": combined,
        "issue_number": issue.get("issue_number"),
        "triggering_comment_body": comment_body or "",
        "triggering_commenter": commenter or "",
        "run_id": run_id(events),
        "correlation_id": correlation_id(events),
        "actor_id": "state_router",
    }
