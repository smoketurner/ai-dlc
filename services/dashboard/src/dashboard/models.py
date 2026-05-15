"""Pydantic response models for the dashboard's JSON API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    """Strict, frozen base."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RunSummary(_Frozen):
    """Compact run row for the runs list.

    ``status`` is the latest event type — the dashboard's display state
    is derived from it via :mod:`dashboard.state_progress`. Per-revision
    counters, verdicts, and severity totals are now derived on demand
    from the event log on the detail page, not denormalised here.
    """

    run_id: str
    project_slug: str
    status: str
    created_at: datetime | None = None
    updated_at: str | None = None
    total_token_in: int = 0
    total_token_out: int = 0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    target_repo: str | None = None
    source_issue_url: str | None = None
    issue_title: str | None = None
    pr_url: str | None = None


class EventLink(_Frozen):
    """A clickable GitHub artifact extracted from an event payload."""

    label: str
    url: str


class RunEvent(_Frozen):
    """One event in a run's timeline."""

    event_id: str
    type: str
    timestamp: str
    payload: dict[str, Any]
    links: list[EventLink] = []


class Critique(_Frozen):
    """Architect-spec critique attached to a run, parsed from S3."""

    summary: str
    issue_count: int = 0
    high_severity_count: int = 0
    medium_severity_count: int = 0
    low_severity_count: int = 0
    body_html: str


class SubmitRunRequest(_Frozen):
    """POST /v1/runs body. ``project_slug`` is derived from ``target_repo``."""

    intent: str
    requestor: str
    target_repo: str
    idempotency_key: str | None = None


class SubmitRunResponse(_Frozen):
    """POST /v1/runs response."""

    run_id: str
    correlation_id: str
    project_slug: str
