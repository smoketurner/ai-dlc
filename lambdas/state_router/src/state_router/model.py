"""Parsed view of one run from DynamoDB.

The router's dispatch handlers operate on this dataclass rather than
raw DDB items so they stay pure functions: ``Run -> Action``. Parsing
DDB items into typed objects also gives us a single place to handle
attribute-name drift (the raw DDB items are just dicts).

The platform consolidated to one issue → one impl PR: there are no
TASK rows any more. All run-level state lives on the ``sk=STATE`` row.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer

from common.state import RunState

DESERIALIZER = TypeDeserializer()


@dataclass(frozen=True, slots=True)
class Run:
    """Parsed run state, used as the input to every dispatch handler.

    The fields fall into a few groups:

    * **identity / provenance** — ``run_id``, ``correlation_id``,
      ``project_slug``, ``target_repo``, ``source_issue_*``.
    * **artifact pointers** — ``plan_s3_key`` (set on DESIGN.READY),
      ``critique_s3_key`` (set on CRITIQUE.READY), ``pr_url``
      (set on IMPL_PR.OPENED).
    * **validation outputs** — ``reviewer_verdict`` (REVIEW.READY),
      ``check_state`` (CHECKS.PASSED / CHECKS.FAILED).
    * **revision bookkeeping** — ``pending_revision_feedback`` (consumed
      and cleared each time the implementer is dispatched in
      ``mode=revision``), ``revision_count`` (incremented per automated
      revision dispatch; not incremented for human-mention revisions).
    """

    run_id: str
    correlation_id: str
    project_slug: str
    intent: str
    requestor: str
    actor_id: str
    current_state: RunState | None
    triage_action: str | None = None
    target_repo: str | None = None
    requestor_sub: str | None = None
    source_issue_url: str | None = None
    source_issue_title: str | None = None
    source_issue_body: str | None = None
    issue_number: int | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    issue_labels: tuple[str, ...] = ()
    triggering_comment_body: str | None = None
    triggering_commenter: str | None = None
    plan_s3_key: str | None = None
    critique_s3_key: str | None = None
    pr_url: str | None = None
    reviewer_verdict: str = ""
    check_state: str = ""
    pending_revision_feedback: tuple[dict[str, Any], ...] = ()
    revision_count: int = 0
    dispatch_failure_count: int = 0


def deserialize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Deserialize a raw DynamoDB item map into native Python values."""
    return {k: DESERIALIZER.deserialize(v) for k, v in item.items()}


def as_int(value: Any) -> int | None:
    """Coerce a deserialized DDB ``N`` value (``Decimal``) to ``int``."""
    return int(value) if isinstance(value, Decimal) else None


def as_str_tuple(value: Any) -> tuple[str, ...]:
    """Sort a deserialized DDB ``SS`` value into a deterministic tuple."""
    return tuple(sorted(value)) if isinstance(value, set) else ()


def normalize_feedback(items: Any) -> tuple[dict[str, Any], ...]:
    """Convert nested ``Decimal`` values to ``int`` inside feedback rows.

    ``pending_revision_feedback`` is shipped to the Implementer as JSON;
    downstream JSON encoders don't accept ``Decimal``, and the implementer
    expects plain ``int`` for numeric fields (``comment_id``, ``review_id``,
    line numbers).
    """
    if not isinstance(items, list):
        return ()
    return tuple(
        {k: int(v) if isinstance(v, Decimal) else v for k, v in row.items()} for row in items
    )


def parse_run(item: dict[str, Any], _task_items: list[dict[str, Any]] | None = None) -> Run | None:
    """Build a :class:`Run` from the run's STATE row.

    Returns ``None`` if the STATE row is missing — the caller (router)
    treats that as an orphan beacon and deletes it.

    The second parameter is kept for handler-compatibility (the SQS
    record reader still does a single Query that returns whatever rows
    exist for the ``pk``); task rows no longer exist in the new world,
    so we simply ignore them.
    """
    if not item:
        return None
    data = deserialize_item(item)
    state = data.get("current_state")
    pk = data.get("pk") or ""
    return Run(
        run_id=data.get("run_id") or pk.removeprefix("RUN#"),
        correlation_id=data.get("correlation_id") or "",
        project_slug=data.get("project_slug") or "",
        intent=data.get("intent") or "",
        requestor=data.get("requestor") or "",
        actor_id=data.get("actor_id") or "system",
        current_state=RunState(state) if state else None,
        triage_action=data.get("triage_action"),
        target_repo=data.get("target_repo"),
        requestor_sub=data.get("requestor_sub"),
        source_issue_url=data.get("source_issue_url"),
        source_issue_title=data.get("source_issue_title"),
        source_issue_body=data.get("source_issue_body"),
        issue_number=as_int(data.get("issue_number")),
        issue_title=data.get("issue_title"),
        issue_body=data.get("issue_body"),
        issue_labels=as_str_tuple(data.get("issue_labels")),
        triggering_comment_body=data.get("triggering_comment_body"),
        triggering_commenter=data.get("triggering_commenter"),
        plan_s3_key=data.get("plan_s3_key"),
        critique_s3_key=data.get("critique_s3_key"),
        pr_url=data.get("pr_url"),
        reviewer_verdict=str(data.get("reviewer_verdict") or ""),
        check_state=str(data.get("check_state") or ""),
        pending_revision_feedback=normalize_feedback(data.get("pending_revision_feedback")),
        revision_count=as_int(data.get("revision_count")) or 0,
        dispatch_failure_count=as_int(data.get("dispatch_failure_count")) or 0,
    )
