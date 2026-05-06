"""Thin wrapper around the AgentCore Browser SDK's session lifecycle.

The official ``bedrock_agentcore.tools.browser_client.BrowserClient`` exposes
``start`` / ``stop`` and ``generate_ws_headers``. This module wraps just those
three calls into:

  * a frozen :class:`BrowserSessionInfo` with the session id and the
    SigV4-signed WebSocket coordinates a CDP client (e.g., Playwright) needs
    to drive the session, and
  * a single error type — :class:`AgentCoreBrowserError` — for every botocore
    or signing failure.

Higher-level navigation / evaluation lives in the calling agent (which
imports Playwright directly); this module deliberately stays out of that
layer so common keeps a lean dependency footprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from common.errors import AgentCoreBrowserError

if TYPE_CHECKING:
    from bedrock_agentcore.tools.browser_client import BrowserClient


@dataclass(frozen=True, slots=True)
class BrowserSessionInfo:
    """Coordinates a CDP client needs to attach to a live browser session.

    Attributes:
        browser_id: The AgentCore Browser resource id (the ``AIDLC_BROWSER_ID``
            env var).
        session_id: The session id minted by ``StartBrowserSession``.
        ws_url: ``wss://...`` URL of the automation stream.
        ws_headers: SigV4-signed headers to include on the WebSocket
            handshake. Treat as a credential — do not log.
    """

    browser_id: str
    session_id: str
    ws_url: str
    ws_headers: dict[str, str]


def start_session(
    client: BrowserClient,
    /,
    *,
    browser_id: str,
    name: str | None = None,
    session_timeout_seconds: int = 600,
) -> BrowserSessionInfo:
    """Start a browser session and return its WebSocket coordinates.

    Args:
        client: A constructed ``BrowserClient`` SDK instance (caller picks
            region + boto3 session).
        browser_id: Resource id of the AgentCore Browser.
        name: Optional session name; the SDK auto-generates one when omitted.
        session_timeout_seconds: Hard idle timeout. Default 600s — high
            enough for a multi-page research run, low enough to release
            sessions quickly on agent crash.
    """
    try:
        session_id = client.start(
            identifier=browser_id,
            name=name,
            session_timeout_seconds=session_timeout_seconds,
        )
        ws_url, headers = client.generate_ws_headers()
    except (BotoCoreError, ClientError, RuntimeError) as exc:
        raise AgentCoreBrowserError(
            "start_session failed",
            browser_id=browser_id,
        ) from exc
    return BrowserSessionInfo(
        browser_id=browser_id,
        session_id=session_id,
        ws_url=ws_url,
        ws_headers=headers,
    )


def stop_session(client: BrowserClient, /) -> None:
    """Stop the active session on ``client``. Idempotent.

    Errors are wrapped so the caller can run this in a ``finally`` block
    without losing the original exception's traceback.
    """
    try:
        client.stop()
    except (BotoCoreError, ClientError) as exc:
        raise AgentCoreBrowserError("stop_session failed") from exc
