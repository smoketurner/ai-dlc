"""Tests for ``reviewer.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reviewer.hooks import READ_PLAN_DOC_CAP, build_hooks


@dataclass
class StubBeforeToolCall:
    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)
    return event


def test_read_plan_doc_cap_is_two() -> None:
    assert READ_PLAN_DOC_CAP == 2


def test_second_read_plan_doc_is_allowed() -> None:
    counter = build_hooks()[0]
    for _ in range(2):
        event = call(counter, "read_plan_doc")
        assert event.cancel_tool is None


def test_third_read_plan_doc_is_denied() -> None:
    counter = build_hooks()[0]
    for _ in range(2):
        call(counter, "read_plan_doc")
    third = call(counter, "read_plan_doc")
    assert third.cancel_tool is not None


def test_other_tools_are_unbounded() -> None:
    counter = build_hooks()[0]
    for _ in range(10):
        event = call(counter, "read_memory_md")
        assert event.cancel_tool is None
