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
from common.runtime import ImplementerInput
from implementer import app


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
