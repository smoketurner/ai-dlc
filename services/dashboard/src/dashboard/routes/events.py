"""GET /v1/runs/{run_id}/events — polling endpoint over the runs read-model."""

from __future__ import annotations

import structlog
from fastapi import APIRouter

from common.state import TERMINAL_RUN_STATES
from dashboard.auth import CurrentUser
from dashboard.models import RunEvent
from dashboard.repos import get_run_events, get_run_progress
from dashboard.state_progress import progress_dict

router = APIRouter()
logger = structlog.get_logger()


@router.get("/v1/runs/{run_id}/events")
async def list_events(
    run_id: str,
    _user: CurrentUser,
    since: str | None = None,
) -> dict[str, object]:
    """Return events newer than ``since``, plus terminal flag and progress.

    ``since`` is an event UUID (the last event the client has seen);
    when ``terminal`` is true the run is in a terminal state and the
    caller can stop polling. ``progress`` carries the live "currently
    running" panel payload (agent, in-state-since, expected next), or
    ``None`` for steady-cursor or terminal states.
    """
    events: list[RunEvent] = get_run_events(run_id, since_event_id=since)
    state, updated_at = get_run_progress(run_id)
    return {
        "events": [ev.model_dump() for ev in events],
        "terminal": state in TERMINAL_RUN_STATES if state else False,
        "current_state": state.value if state else None,
        "updated_at": updated_at,
        "progress": progress_dict(state, updated_at=updated_at),
    }
