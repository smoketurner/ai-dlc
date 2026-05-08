"""Parsed view of one run + its task rows from DynamoDB.

The router's dispatch handlers operate on these dataclasses rather than
raw DDB items so they stay pure functions: ``Run -> Action``. Parsing
DDB items into typed objects also gives us a single place to handle
attribute-name drift (the raw DDB items are just dicts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from boto3.dynamodb.types import TypeDeserializer

from common.state import RunState, TaskState

DESERIALIZER = TypeDeserializer()


@dataclass(frozen=True, slots=True)
class Task:
    """One task on a run, parsed from ``pk=RUN#{id}, sk=TASK#{task_id}``."""

    task_id: str
    state: TaskState
    pr_url: str | None = None
    pr_number: int | None = None
    iteration_count: int = 0
    delivery_ids: frozenset[str] = field(default_factory=frozenset)
    pending_feedback: tuple[dict[str, Any], ...] = ()
    dispatch_failure_count: int = 0


@dataclass(frozen=True, slots=True)
class Run:
    """Parsed run state, used as the input to every dispatch handler."""

    run_id: str
    correlation_id: str
    project_slug: str
    intent: str
    requestor: str
    actor_id: str
    current_state: RunState | None
    workflow_kind: str | None = None
    triage_action: str | None = None
    target_repo: str | None = None
    requestor_sub: str | None = None
    source_issue_url: str | None = None
    issue_number: int | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    issue_labels: tuple[str, ...] = ()
    triggering_comment_body: str | None = None
    triggering_commenter: str | None = None
    spec_slug: str | None = None
    spec_s3_prefix: str | None = None
    pr_url: str | None = None
    synthetic_spec_slug: str | None = None
    task_ids: tuple[str, ...] = ()
    tasks: tuple[Task, ...] = ()
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


def as_str_frozenset(value: Any) -> frozenset[str]:
    """Wrap a deserialized DDB ``SS`` value as a ``frozenset[str]``."""
    return cast("frozenset[str]", frozenset(value)) if isinstance(value, set) else frozenset()


def normalize_feedback(items: Any) -> tuple[dict[str, Any], ...]:
    """Convert nested ``Decimal`` values to ``int`` inside feedback rows.

    ``pending_feedback`` is shipped to the Implementer as JSON; downstream
    JSON encoders don't accept ``Decimal``, and the surrounding code has
    always seen plain ``int`` for numeric fields.
    """
    if not isinstance(items, list):
        return ()
    return tuple(
        {k: int(v) if isinstance(v, Decimal) else v for k, v in row.items()} for row in items
    )


def parse_run(item: dict[str, Any], task_items: list[dict[str, Any]]) -> Run | None:
    """Build a :class:`Run` from the run's STATE row and its task rows.

    Returns ``None`` if the STATE row is missing — the caller (router)
    treats that as an orphan beacon and deletes it.
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
        workflow_kind=data.get("workflow_kind"),
        triage_action=data.get("triage_action"),
        target_repo=data.get("target_repo"),
        requestor_sub=data.get("requestor_sub"),
        source_issue_url=data.get("source_issue_url"),
        issue_number=as_int(data.get("issue_number")),
        issue_title=data.get("issue_title"),
        issue_body=data.get("issue_body"),
        issue_labels=as_str_tuple(data.get("issue_labels")),
        triggering_comment_body=data.get("triggering_comment_body"),
        triggering_commenter=data.get("triggering_commenter"),
        spec_slug=data.get("spec_slug"),
        spec_s3_prefix=data.get("spec_s3_prefix"),
        pr_url=data.get("pr_url"),
        synthetic_spec_slug=data.get("synthetic_spec_slug"),
        task_ids=as_str_tuple(data.get("task_ids")),
        tasks=tuple(parse_task(t) for t in task_items),
        dispatch_failure_count=as_int(data.get("dispatch_failure_count")) or 0,
    )


def parse_task(item: dict[str, Any]) -> Task:
    """Build a :class:`Task` from one ``sk=TASK#{task_id}`` row."""
    data = deserialize_item(item)
    sk = data.get("sk") or ""
    return Task(
        task_id=sk.removeprefix("TASK#"),
        state=TaskState(data.get("status") or "pending"),
        pr_url=data.get("pr_url"),
        pr_number=as_int(data.get("pr_number")),
        iteration_count=as_int(data.get("iteration_count")) or 0,
        delivery_ids=as_str_frozenset(data.get("delivery_ids")),
        pending_feedback=normalize_feedback(data.get("pending_feedback")),
        dispatch_failure_count=as_int(data.get("dispatch_failure_count")) or 0,
    )
