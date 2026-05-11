"""GET /v1/runs/{run_id}/events — polling endpoint over the runs read-model."""

from __future__ import annotations

import structlog
from fastapi import APIRouter

from dashboard.auth import CurrentUser
from dashboard.models import RunEvent
from dashboard.repos import get_run_events, is_run_terminal

router = APIRouter()
logger = structlog.get_logger()


@router.get("/v1/runs/{run_id}/events")
async def list_events(
    run_id: str,
    _user: CurrentUser,
    since: str | None = None,
) -> dict[str, object]:
    """Return events newer than ``since`` plus a terminal flag.

    Clients poll this endpoint with the latest event timestamp as the
    cursor; when ``terminal`` is true the run is in a terminal state
    and the caller can stop polling.
    """
    events: list[RunEvent] = get_run_events(run_id, since_sk=since)
    return {
        "events": [ev.model_dump() for ev in events],
        "terminal": is_run_terminal(run_id),
    }
