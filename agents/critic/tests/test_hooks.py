"""Tests for ``critic.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from critic.hooks import READ_SPEC_DOC_CAP, build_hooks


@dataclass
class StubBeforeToolCall:
    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)
    return event


def test_read_spec_doc_cap_is_three() -> None:
    assert READ_SPEC_DOC_CAP == 3


def test_third_read_spec_doc_is_allowed() -> None:
    counter = build_hooks()[0]
    for _ in range(3):
        event = call(counter, "read_spec_doc")
        assert event.cancel_tool is None


def test_fourth_read_spec_doc_is_denied() -> None:
    counter = build_hooks()[0]
    for _ in range(3):
        call(counter, "read_spec_doc")
    fourth = call(counter, "read_spec_doc")
    assert fourth.cancel_tool is not None


def test_read_memory_md_is_unbounded() -> None:
    counter = build_hooks()[0]
    for _ in range(10):
        event = call(counter, "read_memory_md")
        assert event.cancel_tool is None
