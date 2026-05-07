"""Parsed view of one run + its task rows from DynamoDB.

The router's dispatch handlers operate on these dataclasses rather than
raw DDB items so they stay pure functions: ``Run -> Action``. Parsing
DDB items into typed objects also gives us a single place to handle
attribute-name drift (the raw DDB items are just dicts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.state import RunState, TaskState


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
    target_repo: str | None = None
    requestor_sub: str | None = None
    source_issue_url: str | None = None
    spec_slug: str | None = None
    spec_s3_prefix: str | None = None
    spec_pr_url: str | None = None
    synthetic_spec_slug: str | None = None
    task_ids: tuple[str, ...] = ()
    tasks: tuple[Task, ...] = ()


def parse_run(item: dict[str, Any], task_items: list[dict[str, Any]]) -> Run | None:
    """Build a :class:`Run` from the run's STATE row and its task rows.

    Returns ``None`` if the STATE row is missing — the caller (router)
    treats that as an orphan beacon and deletes it.
    """
    if not item:
        return None
    state_str = ddb_str(item.get("current_state"))
    return Run(
        run_id=ddb_str(item.get("run_id")) or "",
        correlation_id=ddb_str(item.get("correlation_id")) or "",
        project_slug=ddb_str(item.get("project_slug")) or "",
        intent=ddb_str(item.get("intent")) or "",
        requestor=ddb_str(item.get("requestor")) or "",
        actor_id=ddb_str(item.get("actor_id")) or "system",
        current_state=RunState(state_str) if state_str else None,
        workflow_kind=ddb_str(item.get("workflow_kind")),
        target_repo=ddb_str(item.get("target_repo")),
        requestor_sub=ddb_str(item.get("requestor_sub")),
        source_issue_url=ddb_str(item.get("source_issue_url")),
        spec_slug=ddb_str(item.get("spec_slug")),
        spec_s3_prefix=ddb_str(item.get("spec_s3_prefix")),
        spec_pr_url=ddb_str(item.get("spec_pr_url")),
        synthetic_spec_slug=ddb_str(item.get("synthetic_spec_slug")),
        task_ids=tuple(ddb_str_set(item.get("task_ids"))),
        tasks=tuple(parse_task(t) for t in task_items),
    )


def parse_task(item: dict[str, Any]) -> Task:
    """Build a :class:`Task` from one ``sk=TASK#{task_id}`` row."""
    sk = ddb_str(item.get("sk")) or ""
    task_id = sk.removeprefix("TASK#")
    return Task(
        task_id=task_id,
        state=TaskState(ddb_str(item.get("status")) or "pending"),
        pr_url=ddb_str(item.get("pr_url")),
        pr_number=ddb_int(item.get("pr_number")),
        iteration_count=ddb_int(item.get("iteration_count")) or 0,
        delivery_ids=frozenset(ddb_str_set(item.get("delivery_ids"))),
        pending_feedback=tuple(ddb_list(item.get("pending_feedback"))),
    )


def ddb_str(attr: dict[str, Any] | None) -> str | None:
    """Read a DynamoDB ``S`` attribute as ``str | None``."""
    if attr is None:
        return None
    return attr.get("S")


def ddb_int(attr: dict[str, Any] | None) -> int | None:
    """Read a DynamoDB ``N`` attribute as ``int | None``."""
    if attr is None:
        return None
    raw = attr.get("N")
    return int(raw) if raw is not None else None


def ddb_str_set(attr: dict[str, Any] | None) -> list[str]:
    """Read a DynamoDB ``SS`` (string set) as a list."""
    if attr is None:
        return []
    return list(attr.get("SS") or [])


def ddb_list(attr: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read a DynamoDB ``L`` (list of maps) as a list of plain dicts."""
    if attr is None:
        return []
    raw = attr.get("L") or []
    return [unmap(item.get("M") or {}) for item in raw]


def unmap(item: dict[str, Any]) -> dict[str, Any]:
    """Decode a single-level DDB map into a plain dict.

    Sufficient for ``pending_feedback`` entries (FeedbackItem JSON).
    Nested maps would need recursion; we don't need that here.
    """
    out: dict[str, Any] = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            out[k] = int(v["N"])
        elif "BOOL" in v:
            out[k] = v["BOOL"]
        elif "NULL" in v:
            out[k] = None
    return out
