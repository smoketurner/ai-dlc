"""DynamoDB read helpers backing the dashboard pages + JSON routes."""

from __future__ import annotations

import json
from typing import Any

from dashboard.deps import ddb, settings
from dashboard.models import RunEvent, RunSummary

TERMINAL_TYPES = frozenset({"RUN.COMPLETED", "RUN.FAILED"})


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
    """Fetch events for ``run_id`` ordered by sk; filter sks <= ``since_sk`` if given."""
    cfg = settings()
    expr = "pk = :p AND begins_with(sk, :prefix)"
    values: dict[str, Any] = {":p": {"S": f"RUN#{run_id}"}, ":prefix": {"S": "EVENT#"}}
    if since_sk is not None:
        expr += " AND sk > :since"
        values[":since"] = {"S": since_sk}
    resp = ddb().query(
        TableName=cfg.runs_table,
        KeyConditionExpression=expr,
        ExpressionAttributeValues=values,
    )
    return [event_from_item(item) for item in resp.get("Items", [])]


def run_summary_from_item(item: dict[str, Any]) -> RunSummary:
    """Convert a runs-table item into a :class:`RunSummary`."""
    return RunSummary(
        run_id=item["pk"]["S"].removeprefix("RUN#"),
        project_slug=item.get("project_slug", {}).get("S", ""),
        status=item.get("status", {}).get("S", "UNKNOWN"),
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


def is_terminal_event(ev: RunEvent) -> bool:
    """Whether ``ev.type`` is a terminal run state."""
    return ev.type in TERMINAL_TYPES
