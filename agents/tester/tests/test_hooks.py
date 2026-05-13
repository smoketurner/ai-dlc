"""Tests for ``tester.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tester.hooks import GET_ARTIFACT_CAP, build_hooks


@dataclass
class StubBeforeToolCall:
    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)
    return event


def test_get_artifact_cap_is_two() -> None:
    assert GET_ARTIFACT_CAP == 2


def test_second_get_artifact_is_allowed() -> None:
    counter = build_hooks()[0]
    for _ in range(2):
        event = call(counter, "get_artifact")
        assert event.cancel_tool is None


def test_third_get_artifact_is_denied() -> None:
    counter = build_hooks()[0]
    for _ in range(2):
        call(counter, "get_artifact")
    third = call(counter, "get_artifact")
    assert third.cancel_tool is not None


def test_other_tools_are_unbounded() -> None:
    counter = build_hooks()[0]
    for _ in range(10):
        event = call(counter, "read_memory_md")
        assert event.cancel_tool is None
