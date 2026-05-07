"""The Triage agent emits ISSUE.TRIAGED before returning its result."""

from __future__ import annotations

from typing import Any

import pytest

from common.events import EventEnvelope, IssueTriaged
from common.runtime import TriageInput, TriageResult
from triage import app


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to common.event_emit.publish."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    return out


def test_publish_issue_triaged_builds_envelope(captured: list[EventEnvelope[Any]]) -> None:
    payload = TriageInput(
        project_slug="demo",
        target_repo="owner/repo",
        issue_url="https://github.com/owner/repo/issues/1",
        issue_number=1,
        issue_title="bug",
        issue_body="describe",
        run_id="r-1",
        correlation_id="c-1",
    )
    result = TriageResult(
        decision_s3_key="runs/r-1/triage.json",
        action="proceed",
        workflow_kind="bug_fix",
        rationale="clear bug",
        confidence=0.9,
        session_id="r-1",
    )

    app.publish_issue_triaged(payload, result)

    assert len(captured) == 1
    env = captured[0]
    assert env.type == "ISSUE.TRIAGED"
    assert env.actor_id == "triage"
    assert isinstance(env.payload, IssueTriaged)
    assert env.payload.target_repo == "owner/repo"
    assert env.payload.action == "proceed"
    assert env.payload.workflow_kind == "bug_fix"
    assert env.payload.issue_number == 1
