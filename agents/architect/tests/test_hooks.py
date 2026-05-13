"""Tests for ``architect.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from architect.hooks import build_hooks


@dataclass
class StubBeforeToolCall:
    """Duck-typed stand-in for ``strands.hooks.BeforeToolCallEvent``."""

    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)
    return event


def test_build_hooks_returns_one_require_prior_call() -> None:
    hooks = build_hooks()
    assert len(hooks) == 1


def test_put_artifact_blocked_before_read_memory_md() -> None:
    hooks = build_hooks()
    require = hooks[0]
    event = call(require, "put_artifact")
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool


def test_put_artifact_allowed_after_read_memory_md() -> None:
    hooks = build_hooks()
    require = hooks[0]
    call(require, "read_memory_md")
    event = call(require, "put_artifact")
    assert event.cancel_tool is None


def test_each_invocation_gets_independent_hook_instances() -> None:
    """Two calls to build_hooks should not share state."""
    a = build_hooks()[0]
    b = build_hooks()[0]
    call(a, "read_memory_md")
    event_b = call(b, "put_artifact")
    assert event_b.cancel_tool is not None
