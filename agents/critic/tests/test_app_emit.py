"""The Critic emits CRITIQUE.READY before returning its result."""

from __future__ import annotations

from typing import Any

import pytest

from common.events import CritiqueReady, EventEnvelope
from common.runtime import CriticInput, CriticResult
from critic import app


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to common.event_emit.publish."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    return out


def test_publish_critique_ready_builds_envelope(captured: list[EventEnvelope[Any]]) -> None:
    payload = CriticInput(
        project_slug="demo",
        spec_slug="add-healthz",
        spec_s3_prefix="specs/add-healthz/",
        intent="Add /healthz",
        run_id="r-1",
        correlation_id="c-1",
    )
    result = CriticResult(
        spec_slug="add-healthz",
        critique_s3_key="runs/r-1/critique.md",
        issue_count=3,
        high_severity_count=1,
        medium_severity_count=1,
        low_severity_count=1,
        summary="ok",
        session_id="r-1",
        token_in=5,
        token_out=10,
    )

    app.publish_critique_ready(payload, result)

    assert len(captured) == 1
    env = captured[0]
    assert env.type == "CRITIQUE.READY"
    assert env.actor_id == "critic"
    assert isinstance(env.payload, CritiqueReady)
    assert env.payload.issue_count == 3
    assert env.payload.high_severity_count == 1
