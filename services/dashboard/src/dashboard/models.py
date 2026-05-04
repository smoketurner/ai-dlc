"""Pydantic response models for the dashboard's JSON API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    """Strict, frozen base."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RunSummary(_Frozen):
    """Compact run row for the runs list."""

    run_id: str
    project_slug: str
    status: str
    created_at: datetime | None = None
    spec_slug: str | None = None
    tasks_completed: int = 0
    tasks_total: int = 0


class RunEvent(_Frozen):
    """One event in a run's timeline."""

    event_id: str
    type: str
    timestamp: str
    payload: dict[str, Any]


class PendingApproval(_Frozen):
    """One pending HITL gate."""

    run_id: str
    gate_ref: str
    project_slug: str
    pr_url: str | None = None
    summary: str | None = None
    requested_at: datetime | None = None


class SubmitRunRequest(_Frozen):
    """POST /v1/runs body."""

    project_slug: str
    intent: str
    requestor: str
    target_repo: str
    idempotency_key: str | None = None


class SubmitRunResponse(_Frozen):
    """POST /v1/runs response."""

    run_id: str
    correlation_id: str
    project_slug: str
