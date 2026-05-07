"""Server-rendered HTML pages — runs list, run detail, submit."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dashboard.artifacts import read_critique
from dashboard.auth import CurrentUser
from dashboard.deps import ddb, settings
from dashboard.github_repos import repos_for_user
from dashboard.repos import (
    TERMINAL_STATES,
    get_run_events,
    list_recent_runs,
    run_summary_from_item,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/", response_class=HTMLResponse)
async def runs_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """Recent runs list."""
    runs = list_recent_runs(limit=50)
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": runs, "user": user, "terminal_states": TERMINAL_STATES},
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, run_id: str, user: CurrentUser) -> HTMLResponse:
    """Pipeline timeline for one run."""
    events = get_run_events(run_id)
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    summary = first_known_run(run_id, events)
    critique = read_critique(run_id)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run_id": run_id,
            "events": events,
            "summary": summary,
            "critique": critique,
            "user": user,
            "terminal_states": TERMINAL_STATES,
        },
    )


@router.get("/submit", response_class=HTMLResponse)
async def submit_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """Form: new run."""
    target_repos = repos_for_user(user.sub)
    return templates.TemplateResponse(
        request,
        "submit.html",
        {"user": user, "target_repos": target_repos},
    )


@router.get("/healthz", response_class=HTMLResponse)
async def healthz() -> HTMLResponse:
    """ALB health-check endpoint."""
    return HTMLResponse("ok")


def first_known_run(run_id: str, events: list) -> dict[str, str]:  # type: ignore[type-arg]
    """Build a run summary from the STATE row, falling back to event payload.

    The STATE row carries ``current_state`` (state-machine cursor), which
    drives terminal-state UX. Synthesising from events alone misses it.
    """
    cfg = settings()
    item = (
        ddb()
        .get_item(
            TableName=cfg.runs_table,
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
        )
        .get("Item")
    )
    if item:
        return run_summary_from_item(item).model_dump()
    payload = events[-1].payload if events else {}
    return run_summary_from_item(
        {
            "pk": {"S": f"RUN#{run_id}"},
            "project_slug": {"S": str(payload.get("project_slug", ""))},
            "status": {"S": events[-1].type if events else "UNKNOWN"},
        },
    ).model_dump()
