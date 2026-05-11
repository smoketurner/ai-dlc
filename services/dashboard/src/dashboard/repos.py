"""DynamoDB read helpers backing the dashboard pages + JSON routes."""

from __future__ import annotations

import json
from typing import Any

from common.state import TERMINAL_RUN_STATES, RunState
from dashboard.deps import ddb, settings
from dashboard.models import RunEvent, RunSummary

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


def get_run_events(run_id: str, *, since_sk: str | None = None) -> list[RunEvent]:
    """Fetch events for ``run_id`` ordered by sk; exclude sks <= ``since_sk`` if given.

    A DDB ``KeyConditionExpression`` may only carry a single sort-key
    condition, so we cannot mix ``begins_with(sk, "EVENT#")`` with
    ``sk > :since``. We bound to the ``EVENT#`` prefix via ``BETWEEN``
    and post-filter inclusively when a cursor is provided.
    """
    cfg = settings()
    lower = since_sk if since_sk is not None else "EVENT#"
    upper = "EVENT$"  # one byte past every possible "EVENT#..." sk
    resp = ddb().query(
        TableName=cfg.runs_table,
        KeyConditionExpression="pk = :p AND sk BETWEEN :lo AND :hi",
        ExpressionAttributeValues={
            ":p": {"S": f"RUN#{run_id}"},
            ":lo": {"S": lower},
            ":hi": {"S": upper},
        },
    )
    items = resp.get("Items", [])
    if since_sk is not None:
        items = [item for item in items if item["sk"]["S"] > since_sk]
    return [event_from_item(item) for item in items]


def run_summary_from_item(item: dict[str, Any]) -> RunSummary:
    """Convert a runs-table item into a :class:`RunSummary`."""
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
    )


def event_from_item(item: dict[str, Any]) -> RunEvent:
    """Convert a runs-table event row into a :class:`RunEvent`."""
    envelope: dict[str, Any] = json.loads(item.get("envelope", {}).get("S", "{}"))
    return RunEvent(
        event_id=envelope.get("event_id", "unknown"),
        type=item.get("type", {}).get("S", envelope.get("type", "UNKNOWN")),
        timestamp=envelope.get("timestamp", ""),
        payload=envelope.get("payload", {}),
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
