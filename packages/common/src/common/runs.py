"""Shared entry-path primitive for accepting a new run.

Three surfaces start a run: the API Gateway entry adapter Lambda, the
dashboard's ``POST /v1/runs``, and the GitHub webhook handler when an
issue trigger fires. Each one publishes ``REQUEST.RECEIVED`` to
EventBridge — that's it.

Downstream:

* The event_projector receives the EventBridge event and atomically
  writes the EVENT row + SUMMARY row in DynamoDB.
* The DDB Stream → EventBridge Pipe → SQS forwards the EVENT row
  INSERT to the state-router queue, generating a beacon.
* The state-router reads the event log and dispatches the next agent.

There's no entry-side DDB write and no manual SQS beacon — the
projector and the Pipe own the read-model + wake-up edge respectively.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from common.event_emit import publish
from common.events import EventEnvelope, RequestReceived
from common.ids import (
    CorrelationId,
    RunId,
    new_correlation_id,
    new_event_id,
    new_run_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssueContext:
    """Issue-driven trigger context — passed inline on the REQUEST.RECEIVED payload.

    The Triage agent's ``TriageInput`` requires every one of these fields,
    so the webhook captures them at trigger time. The state-router's
    payload builder reads them off the REQUEST.RECEIVED event when
    dispatching triage.

    ``triggering_comment_body`` / ``triggering_commenter`` are populated
    when the run was minted from an ``issue_comment`` event (a reply with
    ``/aidlc go`` or an ``@aidlc-bot`` mention) so the downstream agent
    can read the human's free-form ask alongside the original issue body.
    Empty for runs minted from the initial issue assignment / opening.
    """

    issue_url: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_labels: tuple[str, ...] = ()
    triggering_comment_body: str = ""
    triggering_commenter: str = ""


def start_run(  # noqa: PLR0913
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
    """Mint a run and publish ``REQUEST.RECEIVED``.

    ``run_id`` and ``correlation_id`` are accepted to support callers
    that already minted them (e.g., for idempotent reservation in the
    dashboard); they default to fresh UUID7s.

    For issue-driven runs, the caller supplies an :class:`IssueContext`
    carrying every field the Triage agent's ``TriageInput`` requires —
    the fields ride on the ``REQUEST.RECEIVED`` payload so the router
    can build the triage dispatch from the event log alone.
    """
    rid = run_id or new_run_id()
    cid = correlation_id or new_correlation_id()
    actor = actor_id or requestor
    publish(
        EventEnvelope[RequestReceived](
            event_id=new_event_id(),
            type="REQUEST.RECEIVED",
            run_id=rid,
            correlation_id=cid,
            actor_id=actor,
            payload=RequestReceived(
                project_slug=project_slug,
                intent=intent,
                requestor=requestor,
                requestor_sub=requestor_sub,
                target_repo=target_repo,
                source_issue_url=issue.issue_url if issue else None,
                issue_number=issue.issue_number if issue else None,
                issue_title=issue.issue_title if issue else None,
                issue_body=issue.issue_body if issue else None,
                issue_labels=list(issue.issue_labels) if issue else [],
                triggering_comment_body=issue.triggering_comment_body if issue else None,
                triggering_commenter=issue.triggering_commenter if issue else None,
            ),
        ),
    )
    logger.info(
        "run started",
        extra={
            "run_id": str(rid),
            "project_slug": project_slug,
            "source_issue_url": issue.issue_url if issue else None,
        },
    )
    return rid, cid
