"""GET /v1/runs/{run_id}/stream — Server-Sent Events poller backed by DDB."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter
from sse_starlette import EventSourceResponse

from dashboard.auth import CurrentUser
from dashboard.repos import get_run_events, is_run_terminal

router = APIRouter()
logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 1.0
MAX_DURATION_SECONDS = 60 * 30  # 30 minutes — enough for one run to terminate.


@router.get("/v1/runs/{run_id}/stream")
async def stream(run_id: str, _user: CurrentUser) -> EventSourceResponse:
    """Stream new run events as they land in the read-model."""

    async def gen() -> AsyncIterator[dict[str, str]]:
        last_sk: str | None = None
        elapsed = 0.0
        while elapsed < MAX_DURATION_SECONDS:
            events = get_run_events(run_id, since_sk=last_sk)
            for ev in events:
                yield {"event": ev.type, "data": ev.model_dump_json()}
            if events:
                last_sk = max(events, key=lambda e: e.timestamp).timestamp
                # The state-machine cursor is the source of truth for
                # "done" — covers cancelled (RUN.CANCEL_REQUESTED →
                # cancelled) and rejection-induced failures (SPEC.REJECTED
                # → failed) that the event types alone don't disambiguate.
                if is_run_terminal(run_id):
                    yield {"event": "close", "data": ""}
                    return
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
        yield {"event": "close", "data": "timeout"}

    return EventSourceResponse(gen())
