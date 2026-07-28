"""Cascade-failure protection for the Implementer's fallback RUN.FAILED emit.

Regression for issue #95: ``publish_run_failed`` calls ``try_publish`` so a
second EventBridge rejection on the fallback path is logged rather than
propagated out of the daemon thread (which would wedge the run with only the
``IMPLEMENTER.DISPATCHED`` marker).
"""

from __future__ import annotations

from typing import Any

import pytest

from common import event_emit
from common.events import EventEnvelope
from common.runtime import ImplementerInput
from implementer import app


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
    payload = ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="implementation",
    )

    app.publish_run_failed(payload, RuntimeError("simulated agent crash"))


def test_publish_run_failed_carries_pr_and_issue_urls(
    captured: list[EventEnvelope[Any]],
) -> None:
    """The retrospector dispatcher drops RUN.FAILED with no PR and no issue URL."""
    payload = ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="revision",
        revision_number=1,
        pr_url="https://github.com/owner/name/pull/77",
        source_issue_url="https://github.com/owner/name/issues/42",
    )

    app.publish_run_failed(payload, RuntimeError("revision crashed"))

    assert len(captured) == 1
    failed = captured[0].payload
    assert failed.failed_state == "revising"
    assert failed.pr_url == "https://github.com/owner/name/pull/77"
    assert failed.source_issue_url == "https://github.com/owner/name/issues/42"


def test_publish_run_failed_defaults_urls_to_empty_when_absent(
    captured: list[EventEnvelope[Any]],
) -> None:
    """A programmatic run with no PR yet still emits a valid payload."""
    payload = ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="implementation",
    )

    app.publish_run_failed(payload, RuntimeError("boom"))

    failed = captured[0].payload
    assert failed.pr_url == ""
    assert failed.source_issue_url == ""
