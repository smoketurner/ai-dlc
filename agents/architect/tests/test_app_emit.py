"""The Architect emits DESIGN.READY before returning its result."""

from __future__ import annotations

from typing import Any

import pytest

from architect import app
from common import event_emit
from common.events import DesignReady, EventEnvelope
from common.runtime import ArchitectInput, ArchitectResult


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to ``publish`` or ``try_publish``."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    monkeypatch.setattr(app, "try_publish", out.append)
    return out


def test_publish_design_ready_builds_envelope(captured: list[EventEnvelope[Any]]) -> None:
    payload = ArchitectInput(
        project_slug="demo",
        intent="Add /healthz",
        run_id="r-1",
        correlation_id="c-1",
    )
    result = ArchitectResult(
        plan_s3_key="runs/r-1/plan.md",
        summary="Add a /healthz endpoint to the dashboard service.",
        proposed_adrs=["docs/ADRs/0007-healthz.md"],
        session_id="r-1",
        token_in=10,
        token_out=20,
        cost_usd=0.01,
        duration_ms=500,
    )

    app.publish_design_ready(payload, result)

    assert len(captured) == 1
    env = captured[0]
    assert env.type == "DESIGN.READY"
    assert env.actor_id == "architect"
    assert isinstance(env.payload, DesignReady)
    assert env.payload.plan_s3_key == "runs/r-1/plan.md"
    assert env.payload.summary.startswith("Add a /healthz endpoint")
    assert env.payload.token_in == 10


class RejectingEventsClient:
    """Stub EventBridge client whose put_events always reports a failed entry."""

    def put_events(self, **_: Any) -> dict[str, Any]:
        return {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "ThrottlingException", "ErrorMessage": "Rate exceeded"}],
        }


def test_publish_run_failed_does_not_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    """If EventBridge rejects RUN.FAILED, the fallback must not propagate (issue #95)."""
    monkeypatch.setenv("AIDLC_BUS_NAME", "aidlc-bus-test")
    monkeypatch.setattr(event_emit, "events_client", RejectingEventsClient)
    payload = ArchitectInput(
        project_slug="demo",
        intent="Add /healthz",
        run_id="r-1",
        correlation_id="c-1",
    )

    app.publish_run_failed(payload, RuntimeError("simulated agent crash"))
