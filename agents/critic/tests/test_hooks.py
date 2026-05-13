"""Tests for ``critic.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from critic.hooks import GET_ARTIFACT_CAP, build_hooks


@dataclass
class StubBeforeToolCall:
    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)
    return event


def test_get_artifact_cap_is_four() -> None:
    assert GET_ARTIFACT_CAP == 4


def test_fourth_get_artifact_is_allowed() -> None:
    counter = build_hooks()[0]
    for _ in range(4):
        event = call(counter, "get_artifact")
        assert event.cancel_tool is None


def test_fifth_get_artifact_is_denied() -> None:
    counter = build_hooks()[0]
    for _ in range(4):
        call(counter, "get_artifact")
    fifth = call(counter, "get_artifact")
    assert fifth.cancel_tool is not None


def test_other_tools_are_unbounded() -> None:
    counter = build_hooks()[0]
    for _ in range(10):
        event = call(counter, "browse_url")
        assert event.cancel_tool is None
