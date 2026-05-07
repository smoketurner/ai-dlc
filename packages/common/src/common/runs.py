"""Shared entry-path primitive for accepting a new run.

Three surfaces start a run: the API Gateway entry adapter Lambda, the
dashboard's ``POST /v1/runs``, and the GitHub webhook handler when an
issue trigger fires. Each one needs to do the same three things in the
same order:

  1. Write the run STATE row to the runs DDB table.
  2. Publish ``REQUEST.RECEIVED`` onto the platform EventBridge bus.
  3. Send an SQS beacon so the state-router picks the run up.

If any step is skipped the run sits in the system half-constructed.
This module concentrates the sequence so all three callers behave
identically, and so future entry surfaces don't drift.

The function reads ``AIDLC_RUNS_TABLE``, ``AIDLC_BUS_NAME``, and
``AIDLC_BEACON_QUEUE_URL`` from the environment — the same names the
entry_adapter Lambda and the state_router already use.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog

from common.event_emit import publish
from common.events import EventEnvelope, RequestReceived
from common.ids import (
    CorrelationId,
    RunId,
    new_correlation_id,
    new_event_id,
    new_run_id,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_sqs.client import SQSClient

logger = structlog.get_logger()

# Time the router waits before its first look at the run, in seconds. Long
# enough that the projector has applied REQUEST.RECEIVED → received before
# the router peeks at the STATE row.
BEACON_INITIAL_DELAY_SECONDS = 10


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def sqs() -> SQSClient:
    """Process-cached SQS client."""
    return boto3.client("sqs")


def runs_table() -> str:
    """DynamoDB runs-table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def beacon_queue_url() -> str:
    """SQS beacon queue URL."""
    return os.environ["AIDLC_BEACON_QUEUE_URL"]


def now_iso() -> str:
    """Tz-aware ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class IssueContext:
    """Issue-driven trigger context persisted on the STATE row.

    The Triage agent's ``TriageInput`` requires every one of these fields,
    so the webhook captures them at trigger time and the state-router's
    ``invoke_triage`` reads them off the STATE row when dispatching.
    """

    issue_url: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_labels: tuple[str, ...] = ()


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
    """Mint a run, write the STATE row, emit REQUEST.RECEIVED, send beacon.

    Sequence is strict: DDB ``PutItem`` first (the source of truth), then
    EventBridge ``PutEvents`` (the projector picks up state from here),
    then SQS ``SendMessage`` with a 10s delay (the router's first look).
    Any step raises propagate; the caller decides whether to retry.

    ``run_id`` and ``correlation_id`` are accepted to support callers
    that already minted them (e.g., for idempotent reservation in the
    dashboard); they default to fresh UUID7s.

    For issue-driven runs, the caller supplies an :class:`IssueContext`
    carrying every field the Triage agent's ``TriageInput`` requires —
    those land on the STATE row so the router can rebuild the triage
    payload without re-reading the GitHub API.
    """
    rid = run_id or new_run_id()
    cid = correlation_id or new_correlation_id()
    actor = actor_id or requestor
    write_state_row(
        run_id=str(rid),
        correlation_id=str(cid),
        project_slug=project_slug,
        intent=intent,
        requestor=requestor,
        actor_id=actor,
        target_repo=target_repo,
        requestor_sub=requestor_sub,
        issue=issue,
    )
    envelope = EventEnvelope[RequestReceived](
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
        ),
    )
    publish(envelope)
    send_beacon(run_id=str(rid), project_slug=project_slug)
    logger.info(
        "run started",
        run_id=str(rid),
        project_slug=project_slug,
        source_issue_url=issue.issue_url if issue else None,
    )
    return rid, cid


def write_state_row(  # noqa: PLR0913
    *,
    run_id: str,
    correlation_id: str,
    project_slug: str,
    intent: str,
    requestor: str,
    actor_id: str,
    target_repo: str | None,
    requestor_sub: str | None,
    issue: IssueContext | None,
) -> None:
    """Write the run's STATE row at ``pk=RUN#{run_id}, sk=STATE``.

    ``current_state`` is intentionally absent — the event_projector sets
    it on receipt of ``REQUEST.RECEIVED`` so we keep one writer of run
    state. The ``attribute_not_exists(pk)`` guard prevents clobbering a
    pre-existing row if the same run_id is ever submitted twice (UUID7
    collision is effectively impossible, but we'd rather error than
    overwrite).
    """
    ts = now_iso()
    item: dict[str, dict[str, Any]] = {
        "pk": {"S": f"RUN#{run_id}"},
        "sk": {"S": "STATE"},
        "run_id": {"S": run_id},
        "correlation_id": {"S": correlation_id},
        "project_slug": {"S": project_slug},
        "intent": {"S": intent},
        "requestor": {"S": requestor},
        "actor_id": {"S": actor_id},
        "phase": {"S": "triage"},
        "created_at": {"S": ts},
        "updated_at": {"S": ts},
    }
    if target_repo:
        item["target_repo"] = {"S": target_repo}
    if requestor_sub:
        item["requestor_sub"] = {"S": requestor_sub}
    if issue is not None:
        item["source_issue_url"] = {"S": issue.issue_url}
        item["issue_number"] = {"N": str(issue.issue_number)}
        item["issue_title"] = {"S": issue.issue_title}
        item["issue_body"] = {"S": issue.issue_body}
        if issue.issue_labels:
            item["issue_labels"] = {"SS": list(issue.issue_labels)}
    ddb().put_item(
        TableName=runs_table(),
        Item=item,
        ConditionExpression="attribute_not_exists(pk)",
    )


def send_beacon(*, run_id: str, project_slug: str) -> None:
    """Enqueue the SQS beacon with a short initial delay.

    The 10s delay covers the projector's REQUEST.RECEIVED → received
    transition so the router's first look at the STATE row sees an
    actionable cursor. ``MessageGroupId`` is set to ``project_slug`` so
    SQS fair-queue noisy-neighbor metrics are keyed per project.
    """
    sqs().send_message(
        QueueUrl=beacon_queue_url(),
        MessageBody=json.dumps({"run_id": run_id}),
        DelaySeconds=BEACON_INITIAL_DELAY_SECONDS,
        MessageGroupId=project_slug,
    )
