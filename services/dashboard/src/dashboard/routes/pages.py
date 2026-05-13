"""Server-rendered HTML pages — runs list, run detail, submit."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from common.state import RunState
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
from dashboard.state_progress import progress_dict

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/", response_class=HTMLResponse)
async def runs_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """Recent runs list."""
    runs = list_recent_runs(limit=50)
    runs_progress = _runs_progress(runs)
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": runs,
            "runs_progress": runs_progress,
            "user": user,
            "terminal_states": TERMINAL_STATES,
        },
    )


def _runs_progress(runs: list) -> dict[str, dict[str, object]]:  # type: ignore[type-arg]
    """Map ``run_id`` to its in-flight progress payload (omits inactive runs)."""
    out: dict[str, dict[str, object]] = {}
    for run in runs:
        prog = progress_dict(_coerce_state(run.current_state), updated_at=run.updated_at)
        if prog is not None:
            out[run.run_id] = prog
    return out


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, run_id: str, user: CurrentUser) -> HTMLResponse:
    """Pipeline timeline for one run."""
    events = get_run_events(run_id)
    if not events:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    summary = first_known_run(run_id, events)
    critique = read_critique(run_id)
    failure = run_failure_details(events) if summary.get("current_state") == "failed" else None
    progress = progress_dict(
        _coerce_state(summary.get("current_state")),
        updated_at=summary.get("updated_at"),
    )
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run_id": run_id,
            "events": events,
            "summary": summary,
            "critique": critique,
            "failure": failure,
            "progress": progress,
            "user": user,
            "terminal_states": TERMINAL_STATES,
        },
    )


def _coerce_state(raw: object) -> RunState | None:
    """Parse a STATE-row ``current_state`` string into a ``RunState``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return RunState(raw)
    except ValueError:
        return None


def run_failure_details(events: list) -> dict[str, str] | None:  # type: ignore[type-arg]
    """Pull the most recent ``RUN.FAILED`` payload off the event timeline.

    Surfaces ``failed_state`` / ``error_class`` / ``error_message`` /
    ``retryable`` to the run-detail page so an operator can see why the
    run terminated without dropping into DynamoDB by hand. Returns
    ``None`` when no ``RUN.FAILED`` event is present (run failed via a
    different terminal path).
    """
    for ev in reversed(events):
        if ev.type != "RUN.FAILED":
            continue
        payload = ev.payload or {}
        return {
            "failed_state": str(payload.get("failed_state") or "unknown"),
            "error_class": str(payload.get("error_class") or "unknown"),
            "error_message": str(payload.get("error_message") or ""),
            "retryable": "yes" if payload.get("retryable") else "no",
            "actor_id": str(getattr(ev, "actor_id", "") or ""),
            "timestamp": str(getattr(ev, "timestamp", "") or ""),
        }
    return None


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
