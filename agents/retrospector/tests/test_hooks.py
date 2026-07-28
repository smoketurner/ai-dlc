"""Tests for ``retrospector.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.hooks import RequirePriorCall, ToolCallCounter
from retrospector.hooks import GET_ARTIFACT_CAP, build_hooks


@dataclass
class StubBeforeToolCall:
    """Duck-typed stand-in for ``strands.hooks.BeforeToolCallEvent``."""

    tool_use: dict[str, Any]
    cancel_tool: str | None = None


GATEWAY_NAME = "artifact-tool___artifact_tool"


def call(hook: Any, name: str) -> StubBeforeToolCall:
    """Build a realistic gateway envelope and run ``hook.check``.

    All ops the retrospector gates (``get_artifact``, ``read_memory_md``,
    ``write_memory_md``) are gateway-routed.
    """
    event = StubBeforeToolCall(tool_use={"name": GATEWAY_NAME, "input": {"op": name}})
    hook.check(event)
    return event


def test_build_hooks_returns_counter_and_prior_call() -> None:
    hooks = build_hooks()
    assert len(hooks) == 2
    assert isinstance(hooks[0], ToolCallCounter)
    assert isinstance(hooks[1], RequirePriorCall)


def test_get_artifact_capped_at_limit() -> None:
    counter = build_hooks()[0]
    for _ in range(GET_ARTIFACT_CAP):
        call(counter, "get_artifact")
    over = call(counter, "get_artifact")
    assert over.cancel_tool is not None
    assert f"cap of {GET_ARTIFACT_CAP}" in over.cancel_tool


def test_cap_covers_a_full_revision_cap_capture_round() -> None:
    """A cap-hit RUN.FAILED hands the agent 3 validators x 4 rounds = 12 keys.

    Plus the PR, the issue thread, the plan and MEMORY.md. Truncating
    here is exactly what breaks cross-round pattern detection.
    """
    counter = build_hooks()[0]
    for _ in range(12 + 4):
        assert call(counter, "get_artifact").cancel_tool is None


def test_cap_covers_the_maximum_artifact_key_list() -> None:
    """``RetrospectorInput.validation_artifact_keys`` is bounded at 64."""
    assert GET_ARTIFACT_CAP >= 64


def test_write_memory_md_blocked_before_read_memory_md() -> None:
    require = build_hooks()[1]
    event = call(require, "write_memory_md")
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool


def test_write_memory_md_allowed_after_read_memory_md() -> None:
    require = build_hooks()[1]
    call(require, "read_memory_md")
    event = call(require, "write_memory_md")
    assert event.cancel_tool is None
