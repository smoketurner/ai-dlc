"""The Architect emits SPEC.READY before returning its result.

The full ``handler()`` is integration-shaped — it clones a repo and
runs an LLM. This test isolates :func:`architect.app.publish_spec_ready`
since it's pure data shaping with one observable side effect.
"""

from __future__ import annotations

from typing import Any

import pytest

from architect import app
from common.events import EventEnvelope, SpecReady
from common.runtime import ArchitectInput, ArchitectResult


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to common.event_emit.publish."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    return out


def test_publish_spec_ready_builds_envelope(captured: list[EventEnvelope[Any]]) -> None:
    payload = ArchitectInput(
        project_slug="demo",
        intent="Add /healthz",
        run_id="r-1",
        correlation_id="c-1",
    )
    result = ArchitectResult(
        spec_slug="add-healthz",
        spec_s3_prefix="specs/add-healthz/",
        requirements_summary="r",
        design_summary="d",
        task_count=2,
        task_ids=["T-001", "T-002"],
        proposed_adrs=["ADR-001"],
        session_id="r-1",
        token_in=10,
        token_out=20,
        cost_usd=0.01,
        duration_ms=500,
    )

    app.publish_spec_ready(payload, result)

    assert len(captured) == 1
    env = captured[0]
    assert env.type == "SPEC.READY"
    assert env.actor_id == "architect"
    assert isinstance(env.payload, SpecReady)
    assert env.payload.task_ids == ["T-001", "T-002"]
    assert env.payload.task_count == 2
    assert env.payload.spec_slug == "add-healthz"
    assert env.payload.token_in == 10
