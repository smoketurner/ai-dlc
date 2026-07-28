"""Cascade-failure protection for the Code-Critic's fallback RUN.FAILED emit.

Regression for issue #95: ``publish_run_failed`` calls ``try_publish`` so a
second EventBridge rejection on the fallback path is logged rather than
propagated out of the daemon thread.
"""

from __future__ import annotations

from typing import Any

import pytest

from code_critic import app
from common import event_emit
from common.events import EventEnvelope
from common.runtime import CodeCriticInput


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to ``publish`` or ``try_publish``."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    monkeypatch.setattr(app, "try_publish", out.append)
    return out


class RejectingEventsClient:
    """Stub EventBridge client whose put_events always reports a failed entry."""

    def put_events(self, **_: Any) -> dict[str, Any]:
        return {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "ThrottlingException", "ErrorMessage": "Rate exceeded"}],
        }


def test_publish_run_failed_does_not_cascade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_BUS_NAME", "aidlc-bus-test")
    monkeypatch.setattr(event_emit, "events_client", RejectingEventsClient)
    payload = CodeCriticInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        pr_url="https://github.com/owner/repo/pull/1",
        run_id="r-1",
        correlation_id="c-1",
    )

    app.publish_run_failed(payload, RuntimeError("simulated agent crash"))


def test_publish_run_failed_carries_pr_and_issue_urls(
    captured: list[EventEnvelope[Any]],
) -> None:
    """``CodeCriticInput.pr_url`` is required, so this is always available."""
    payload = CodeCriticInput(
        project_slug="demo",
        plan_s3_key="runs/r-1/plan.md",
        pr_url="https://github.com/owner/repo/pull/1",
        run_id="r-1",
        correlation_id="c-1",
        source_issue_url="https://github.com/owner/repo/issues/9",
    )

    app.publish_run_failed(payload, RuntimeError("critic crashed"))

    assert len(captured) == 1
    failed = captured[0].payload
    assert failed.pr_url == "https://github.com/owner/repo/pull/1"
    assert failed.source_issue_url == "https://github.com/owner/repo/issues/9"
