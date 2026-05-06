"""Tests for ``common.agentcore_browser``.

The wrapper takes a ``BrowserClient`` SDK instance positionally; tests mock
it via ``unittest.mock.MagicMock`` and verify that lifecycle calls round-trip
and errors are wrapped.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from common.agentcore_browser import BrowserSessionInfo, start_session, stop_session
from common.errors import AgentCoreBrowserError


def test_start_session_returns_session_info() -> None:
    client = MagicMock()
    client.start.return_value = "sess-1"
    client.generate_ws_headers.return_value = (
        "wss://example/browser-streams/b-1/sessions/sess-1/automation",
        {"Authorization": "AWS4-HMAC...", "Host": "example"},
    )
    info = start_session(client, browser_id="b-1", session_timeout_seconds=300)
    assert isinstance(info, BrowserSessionInfo)
    assert info.browser_id == "b-1"
    assert info.session_id == "sess-1"
    assert info.ws_url.startswith("wss://")
    assert info.ws_headers["Authorization"].startswith("AWS4-HMAC")
    client.start.assert_called_once_with(
        identifier="b-1",
        name=None,
        session_timeout_seconds=300,
    )


def test_start_session_wraps_client_errors() -> None:
    client = MagicMock()
    client.start.side_effect = ClientError({"Error": {"Code": "Throttle"}}, "Start")
    with pytest.raises(AgentCoreBrowserError) as exc:
        start_session(client, browser_id="b-1")
    assert exc.value.context["browser_id"] == "b-1"


def test_start_session_wraps_runtime_error_from_signing() -> None:
    """The SDK raises RuntimeError when AWS credentials are missing."""
    client = MagicMock()
    client.start.return_value = "sess-1"
    client.generate_ws_headers.side_effect = RuntimeError("No AWS credentials found")
    with pytest.raises(AgentCoreBrowserError):
        start_session(client, browser_id="b-1")


def test_stop_session_calls_stop() -> None:
    client = MagicMock()
    stop_session(client)
    client.stop.assert_called_once_with()


def test_stop_session_wraps_errors() -> None:
    client = MagicMock()
    client.stop.side_effect = ClientError({"Error": {"Code": "X"}}, "Stop")
    with pytest.raises(AgentCoreBrowserError):
        stop_session(client)
