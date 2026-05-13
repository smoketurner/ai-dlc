"""Tests for ``proposer.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.hooks import RequirePriorCall, ToolCallCounter
from proposer.hooks import BROWSE_URL_CAP, GET_ARTIFACT_CAP, build_hooks


@dataclass
class StubBeforeToolCall:
    """Duck-typed stand-in for ``strands.hooks.BeforeToolCallEvent``."""

    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)
    return event


def test_build_hooks_returns_counter_and_prior_call() -> None:
    hooks = build_hooks()
    assert len(hooks) == 2
    assert isinstance(hooks[0], ToolCallCounter)
    assert isinstance(hooks[1], RequirePriorCall)


def test_browse_url_capped_at_limit() -> None:
    counter = build_hooks()[0]
    for _ in range(BROWSE_URL_CAP):
        event = call(counter, "browse_url")
        assert event.cancel_tool is None
    over = call(counter, "browse_url")
    assert over.cancel_tool is not None
    assert f"cap of {BROWSE_URL_CAP}" in over.cancel_tool


def test_get_artifact_capped_at_limit() -> None:
    counter = build_hooks()[0]
    for _ in range(GET_ARTIFACT_CAP):
        call(counter, "get_artifact")
    over = call(counter, "get_artifact")
    assert over.cancel_tool is not None


def test_browse_url_blocked_before_read_memory_md() -> None:
    require = build_hooks()[1]
    event = call(require, "browse_url")
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool


def test_browse_url_allowed_after_read_memory_md() -> None:
    require = build_hooks()[1]
    call(require, "read_memory_md")
    event = call(require, "browse_url")
    assert event.cancel_tool is None
