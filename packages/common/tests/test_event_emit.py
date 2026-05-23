"""Tests for ``common.event_emit.publish``.

Verifies that ``publish()`` raises when EventBridge reports any entry
failure (``FailedEntryCount > 0``). Without this check, transient
EventBridge issues silently drop terminal agent events and wedge runs
in non-terminal states.
"""

from __future__ import annotations

from typing import Any

import pytest

from common import event_emit
from common.errors import EventEmitError
from common.events import EventEnvelope, RequestReceived
from common.ids import new_correlation_id, new_run_id


class FakeEventsClient:
    """Stub EventBridge client whose ``put_events`` response is configurable."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def put_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _envelope() -> EventEnvelope[RequestReceived]:
    return EventEnvelope[RequestReceived](
        type="REQUEST.RECEIVED",
        run_id=new_run_id(),
        correlation_id=new_correlation_id(),
        actor_id="test",
        payload=RequestReceived(project_slug="demo", intent="x", requestor="alice"),
    )


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_BUS_NAME", "aidlc-bus-test")


def _install_fake(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> FakeEventsClient:
    fake = FakeEventsClient(response)
    monkeypatch.setattr(event_emit, "events_client", lambda: fake)
    return fake


def test_publish_succeeds_when_failed_entry_count_is_zero(
    bus: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake(monkeypatch, {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]})
    event_emit.publish(_envelope())
    assert len(fake.calls) == 1


def test_publish_raises_on_failed_entry_count(
    bus: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake(
        monkeypatch,
        {
            "FailedEntryCount": 1,
            "Entries": [
                {
                    "ErrorCode": "ThrottlingException",
                    "ErrorMessage": "Rate exceeded",
                },
            ],
        },
    )
    with pytest.raises(EventEmitError) as excinfo:
        event_emit.publish(_envelope())
    assert excinfo.value.context["error_code"] == "ThrottlingException"
    assert excinfo.value.context["error_message"] == "Rate exceeded"
    assert excinfo.value.context["failed_entry_count"] == 1


def test_publish_raises_when_entries_missing_error_fields(
    bus: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: still raise even if the per-entry error fields are absent."""
    _install_fake(monkeypatch, {"FailedEntryCount": 1, "Entries": [{}]})
    with pytest.raises(EventEmitError) as excinfo:
        event_emit.publish(_envelope())
    assert excinfo.value.context["error_code"] is None
    assert excinfo.value.context["error_message"] is None
