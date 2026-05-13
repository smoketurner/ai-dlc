"""The Architect entrypoint is non-blocking — work runs on a daemon thread.

Pins the AgentCore async-task contract: ``handler()`` must return
``{"status": "dispatched", ...}`` in ~100ms regardless of how long
the actual plan generation takes, and the background thread must
emit either ``DESIGN.READY`` (success) or ``RUN.FAILED`` (exception)
so the run never wedges silently.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from architect import app
from common.events import DesignReady, EventEnvelope, RunFailed
from common.runtime import ArchitectInput, ArchitectResult


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to common.event_emit.publish."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    return out


def architect_event() -> dict[str, Any]:
    """Minimal valid ArchitectInput envelope for the entrypoint."""
    return {
        "project_slug": "demo",
        "intent": "Add /healthz",
        "run_id": "r-1",
        "correlation_id": "c-1",
    }


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it returns truthy or we time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_handler_returns_immediately_on_success(captured: list[EventEnvelope[Any]]) -> None:
    """The entrypoint must return in ~100ms even when the body would take longer."""
    sleeping_completion = []

    def slow_run_architect(payload: ArchitectInput, async_task_id: int) -> None:
        time.sleep(0.05)
        app.publish_design_ready(
            payload,
            ArchitectResult(
                plan_s3_key="runs/r-1/plan.md",
                summary="Add /healthz route.",
                proposed_adrs=[],
                session_id=payload.run_id,
            ),
        )
        sleeping_completion.append(async_task_id)

    with patch.object(app, "run_architect", slow_run_architect):
        start = time.monotonic()
        out = app.handler(architect_event())
        elapsed = time.monotonic() - start

    assert out["status"] == "dispatched"
    assert out["run_id"] == "r-1"
    assert "task_id" in out
    # Entrypoint returns long before the body finishes.
    assert elapsed < 0.5
    # Background thread eventually emits DESIGN.READY.
    assert wait_for(lambda: any(e.type == "DESIGN.READY" for e in captured))


def test_background_exception_emits_run_failed(captured: list[EventEnvelope[Any]]) -> None:
    """When the background thread raises, the entrypoint still returns OK and RUN.FAILED is emitted.

    Without the failure emission, an uncaught exception inside the
    daemon thread would leave the run wedged in ``architect_running``.
    """

    def boom(_payload: ArchitectInput, async_task_id: int) -> None:
        try:
            app.publish_run_failed(
                ArchitectInput(
                    project_slug="demo",
                    intent="Add /healthz",
                    run_id="r-1",
                    correlation_id="c-1",
                ),
                RuntimeError("synthetic failure"),
            )
        finally:
            app.app.complete_async_task(async_task_id)

    with patch.object(app, "run_architect", boom):
        out = app.handler(architect_event())

    assert out["status"] == "dispatched"
    assert wait_for(lambda: any(e.type == "RUN.FAILED" for e in captured))
    failed = next(e for e in captured if e.type == "RUN.FAILED")
    assert isinstance(failed.payload, RunFailed)
    assert failed.payload.failed_state == "architect_running"
    assert failed.payload.error_class == "RuntimeError"


def test_publish_run_failed_builds_envelope(captured: list[EventEnvelope[Any]]) -> None:
    """The RUN.FAILED helper builds a well-typed envelope with the run-id chain."""
    payload = ArchitectInput(
        project_slug="demo",
        intent="Add /healthz",
        run_id="r-7",
        correlation_id="c-7",
    )
    app.publish_run_failed(payload, ValueError("nope"))
    assert len(captured) == 1
    env = captured[0]
    assert env.type == "RUN.FAILED"
    assert env.actor_id == "architect"
    assert isinstance(env.payload, DesignReady) is False
    assert isinstance(env.payload, RunFailed)
    assert env.payload.error_class == "ValueError"
    assert env.payload.error_message == "nope"
    assert env.payload.retryable is True
