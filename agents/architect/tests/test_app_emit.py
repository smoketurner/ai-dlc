"""The Architect emits DESIGN.READY before returning its result."""

from __future__ import annotations

from typing import Any

import pytest

from architect import app
from common.events import DesignReady, EventEnvelope
from common.runtime import ArchitectInput, ArchitectResult


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to common.event_emit.publish."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
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
