"""DynamoDB read helpers backing the dashboard pages + JSON routes."""

from __future__ import annotations

import json
from typing import Any

from common.state import TERMINAL_RUN_STATES, RunState
from dashboard.deps import ddb, settings
from dashboard.models import EventLink, RunEvent, RunSummary, TaskSummary

EVENT_LINK_LABELS: tuple[tuple[str, str], ...] = (
    ("pr_url", "PR"),
    ("issue_url", "issue"),
    ("comment_url", "comment"),
    ("html_url", "link"),
)
"""Payload-key → pill-label mapping for event-row linkification.

Order is render order — the first hit wins for duplicate URLs, and the
list dictates the pill order in the timeline row.
"""

TERMINAL_STATES = frozenset(s.value for s in TERMINAL_RUN_STATES)
"""Stringified ``RunState`` values that mean a run is finished.

Templates and route handlers compare ``run.current_state`` against this
set rather than the ``status`` attribute (which holds the most-recent
event type, not the state-machine cursor). A cancelled run, for
example, has ``status="RUN.CANCEL_REQUESTED"`` and
``current_state="cancelled"`` — only the latter reliably says "done".
"""


def list_recent_runs(*, limit: int = 50) -> list[RunSummary]:
    """Scan the runs table for recent run rows.

    A scan is fine here while the runs table stays small. As soon as we have
    a meaningful production volume, swap to a GSI keyed by status + ts.
    """
    cfg = settings()
    resp = ddb().scan(
        TableName=cfg.runs_table,
        FilterExpression="sk = :state",
        ExpressionAttributeValues={":state": {"S": "STATE"}},
        Limit=limit,
    )
    return [run_summary_from_item(item) for item in resp.get("Items", [])]


def get_run_events(run_id: str, *, since_event_id: str | None = None) -> list[RunEvent]:
    """Fetch events for ``run_id`` ordered by sk; exclude events <= ``since_event_id``.

    Callers pass plain event UUIDs; the ``EVENT#`` sort-key prefix is a
    storage detail kept inside this module. A DDB ``KeyConditionExpression``
    may only carry a single sort-key condition, so we cannot mix
    ``begins_with(sk, "EVENT#")`` with ``sk > :since``. We bound to the
    ``EVENT#`` prefix via ``BETWEEN`` and post-filter inclusively when a
    cursor is provided.
    """
    cfg = settings()
    lower_sk = f"EVENT#{since_event_id}" if since_event_id is not None else "EVENT#"
    upper = "EVENT$"  # one byte past every possible "EVENT#..." sk
    resp = ddb().query(
        TableName=cfg.runs_table,
        KeyConditionExpression="pk = :p AND sk BETWEEN :lo AND :hi",
        ExpressionAttributeValues={
            ":p": {"S": f"RUN#{run_id}"},
            ":lo": {"S": lower_sk},
            ":hi": {"S": upper},
        },
    )
    items = resp.get("Items", [])
    if since_event_id is not None:
        items = [item for item in items if item["sk"]["S"] > lower_sk]
    return [event_from_item(item) for item in items]


def run_summary_from_item(item: dict[str, Any]) -> RunSummary:
    """Convert a runs-table item into a :class:`RunSummary`."""
    issue_number_raw = item.get("issue_number", {}).get("N")
    return RunSummary(
        run_id=item["pk"]["S"].removeprefix("RUN#"),
        project_slug=item.get("project_slug", {}).get("S", ""),
        status=item.get("status", {}).get("S", "UNKNOWN"),
        current_state=item.get("current_state", {}).get("S") or None,
        spec_slug=item.get("spec_slug", {}).get("S") or None,
        tasks_completed=int(item.get("tasks_completed", {}).get("N", "0")),
        tasks_total=int(item.get("tasks_total", {}).get("N", "0")),
        total_token_in=int(item.get("total_token_in", {}).get("N", "0")),
        total_token_out=int(item.get("total_token_out", {}).get("N", "0")),
        total_cost_usd=float(item.get("total_cost_usd", {}).get("N", "0")),
        total_duration_ms=int(item.get("total_duration_ms", {}).get("N", "0")),
        target_repo=item.get("target_repo", {}).get("S") or None,
        source_issue_url=item.get("source_issue_url", {}).get("S") or None,
        issue_number=int(issue_number_raw) if issue_number_raw is not None else None,
        issue_title=item.get("issue_title", {}).get("S") or None,
        pr_url=item.get("pr_url", {}).get("S") or None,
    )


def event_from_item(item: dict[str, Any]) -> RunEvent:
    """Convert a runs-table event row into a :class:`RunEvent`."""
    envelope: dict[str, Any] = json.loads(item.get("envelope", {}).get("S", "{}"))
    payload = envelope.get("payload", {})
    return RunEvent(
        event_id=envelope.get("event_id", "unknown"),
        type=item.get("type", {}).get("S", envelope.get("type", "UNKNOWN")),
        timestamp=envelope.get("timestamp", ""),
        payload=payload,
        links=event_links(payload),
    )


def event_links(payload: dict[str, Any]) -> list[EventLink]:
    """Extract clickable GitHub artifacts from an event payload.

    Recognised keys are listed in :data:`EVENT_LINK_LABELS`. A URL that
    appears under multiple keys (e.g. ``pr_url`` and ``html_url``) is
    only emitted once — the first matching key wins.
    """
    seen: set[str] = set()
    links: list[EventLink] = []
    for key, label in EVENT_LINK_LABELS:
        value = payload.get(key)
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        links.append(EventLink(label=label, url=value))
    return links


def get_run_tasks(run_id: str) -> list[TaskSummary]:
    """Fetch TASK rows for ``run_id`` sorted by ``last_event_at``.

    Returns every task with a ``pr_url``; rejected and closed tasks are
    included so the dashboard preserves a full PR audit trail. Tasks
    without a ``pr_url`` (e.g. still pending) are omitted because they
    have nothing to link to.
    """
    cfg = settings()
    resp = ddb().query(
        TableName=cfg.runs_table,
        KeyConditionExpression="pk = :p AND begins_with(sk, :t)",
        ExpressionAttributeValues={
            ":p": {"S": f"RUN#{run_id}"},
            ":t": {"S": "TASK#"},
        },
    )
    tasks = [task_summary_from_item(item) for item in resp.get("Items", [])]
    tasks = [t for t in tasks if t.pr_url]
    tasks.sort(key=lambda t: t.last_event_at or "")
    return tasks


def task_summary_from_item(item: dict[str, Any]) -> TaskSummary:
    """Convert a TASK row into a :class:`TaskSummary`."""
    return TaskSummary(
        task_id=item["sk"]["S"].removeprefix("TASK#"),
        status=item.get("status", {}).get("S", "unknown"),
        pr_url=item.get("pr_url", {}).get("S") or None,
        last_event_at=item.get("last_event_at", {}).get("S") or None,
    )


def get_run_state(run_id: str) -> RunState | None:
    """Read ``current_state`` off the run's STATE row, or ``None``.

    The state-machine cursor is the source of truth for "is this run
    done?" — separate from the ``status`` attribute (last event type).
    Callers use it to decide SSE close, delete authorization, terminal
    badges in the UI.
    """
    cfg = settings()
    item = (
        ddb()
        .get_item(
            TableName=cfg.runs_table,
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
            ProjectionExpression="current_state",
        )
        .get("Item")
    )
    if not item:
        return None
    raw = item.get("current_state", {}).get("S")
    if not raw:
        return None
    try:
        return RunState(raw)
    except ValueError:
        return None


def is_run_terminal(run_id: str) -> bool:
    """``True`` when the run's state-machine cursor is in a terminal state."""
    state = get_run_state(run_id)
    return state in TERMINAL_RUN_STATES if state else False
