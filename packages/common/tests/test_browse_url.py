"""Tests for ``common.agentcore_browser.browse_url``.

The AgentCore browser SDK and Playwright are mocked. We focus on the
session-lifecycle invariant (``stop_session`` always runs) and on the
two output shapes (default text vs. ``extract_js`` value).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import playwright.sync_api as pw
import pytest
from playwright.sync_api import Error as PlaywrightError

from common import agentcore_browser as b
from common.agentcore_browser import BrowserSessionInfo, browse_url
from common.errors import AgentCoreBrowserError


def make_browser_info() -> BrowserSessionInfo:
    return BrowserSessionInfo(
        browser_id="b-1",
        session_id="s-1",
        ws_url="wss://example/automation",
        ws_headers={"Authorization": "AWS4-HMAC..."},
    )


def make_chromium(page_returns: dict[str, Any]) -> MagicMock:
    """Build a MagicMock playwright Browser whose page returns canned values."""
    page = MagicMock()
    page.goto.return_value = None
    page.title.return_value = page_returns.get("title", "Page Title")

    def evaluate_side_effect(expr: str) -> Any:
        if expr == "() => document.body.innerText":
            return page_returns.get("body_text", "default body text")
        return page_returns.get("extract_js_result")

    page.evaluate.side_effect = evaluate_side_effect

    context = MagicMock()
    context.pages = [page]
    chromium = MagicMock()
    chromium.contexts = [context]
    return chromium


def install_browser_session_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: MagicMock,
    start_side_effect: Any | None = None,
) -> list[bool]:
    """Patch BrowserClient + start/stop_session + sync_playwright.

    Returns a list that's appended to whenever ``stop_session`` is called.
    """
    monkeypatch.setattr(b, "BrowserClient", lambda region: MagicMock())
    if start_side_effect is None:
        monkeypatch.setattr(b, "start_session", lambda _client, *, browser_id: make_browser_info())
    else:
        monkeypatch.setattr(b, "start_session", start_side_effect)
    stop_calls: list[bool] = []
    monkeypatch.setattr(b, "stop_session", lambda _client: stop_calls.append(True))
    fake_pw_ctx = MagicMock()
    fake_pw_ctx.__enter__ = lambda self: runner
    fake_pw_ctx.__exit__ = lambda *args: None
    monkeypatch.setattr(pw, "sync_playwright", lambda: fake_pw_ctx)
    return stop_calls


def test_browse_url_returns_error_when_browser_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIDLC_BROWSER_ID", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    out = browse_url("https://duckduckgo.com")
    assert out == {"error": "AIDLC_BROWSER_ID is not set"}


def test_browse_url_returns_text_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_BROWSER_ID", "b-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    chromium = make_chromium({"title": "Anthropic Docs", "body_text": "best practices"})
    runner = MagicMock()
    runner.chromium.connect_over_cdp.return_value = chromium
    stop_calls = install_browser_session_stubs(monkeypatch, runner=runner)

    out = browse_url("https://docs.anthropic.com/en/api")
    assert out["url"] == "https://docs.anthropic.com/en/api"
    assert out["title"] == "Anthropic Docs"
    assert out["text"] == "best practices"
    assert stop_calls == [True]


def test_browse_url_uses_extract_js_when_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_BROWSER_ID", "b-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    chromium = make_chromium({"title": "Foo", "extract_js_result": ["A", "B", "C"]})
    runner = MagicMock()
    runner.chromium.connect_over_cdp.return_value = chromium
    install_browser_session_stubs(monkeypatch, runner=runner)

    out = browse_url(
        "https://owasp.org/Top10",
        extract_js="() => [...document.querySelectorAll('h2')].map(h => h.innerText)",
    )
    assert out["extracted"] == ["A", "B", "C"]
    assert "text" not in out


def test_browse_url_stops_session_on_playwright_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_BROWSER_ID", "b-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    runner = MagicMock()
    runner.chromium.connect_over_cdp.side_effect = PlaywrightError("connection refused")
    stop_calls = install_browser_session_stubs(monkeypatch, runner=runner)

    out = browse_url("https://example.com")
    assert "error" in out
    assert "connection refused" in out["error"]
    assert stop_calls == [True]


def test_browse_url_returns_error_when_start_session_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_BROWSER_ID", "b-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def boom(_client: Any, *, browser_id: str) -> Any:
        raise AgentCoreBrowserError("start failed", browser_id=browser_id)

    install_browser_session_stubs(monkeypatch, runner=MagicMock(), start_side_effect=boom)

    out = browse_url("https://example.com")
    assert "start failed" in out["error"]
