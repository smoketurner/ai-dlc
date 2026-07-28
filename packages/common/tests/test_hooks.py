"""Tests for ``common.hooks``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.hooks import (
    InputValidator,
    RequireAllPriorCalls,
    RequirePriorCall,
    ToolCallCounter,
    effective_tool_name,
)

GATEWAY_NAME = "artifact-tool___artifact_tool"


@dataclass
class StubBeforeToolCall:
    """Minimal stand-in for ``strands.hooks.BeforeToolCallEvent``.

    Strands' hook helpers only read ``tool_use["name"]`` /
    ``tool_use["input"]`` and write ``cancel_tool``, so a duck-typed
    dataclass is enough for unit tests.
    """

    tool_use: dict[str, Any]
    cancel_tool: str | None = None


@dataclass
class StubBeforeInvocation:
    """Minimal stand-in for ``BeforeInvocationEvent`` — payload unused."""

    agent: object = field(default=None)


def _envelope(
    op_or_name: str,
    *,
    gateway: bool,
    extra_input: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a realistic ``tool_use`` envelope.

    Gateway-routed calls carry the composite MCP tool name with the op
    in ``input["op"]`` — matches what Strands delivers in production
    when tools come from ``gateway_tools(mcp_client)``.
    """
    if gateway:
        return {"name": GATEWAY_NAME, "input": {"op": op_or_name, **(extra_input or {})}}
    tool_use: dict[str, Any] = {"name": op_or_name}
    if extra_input is not None:
        tool_use["input"] = extra_input
    return tool_use


def call(counter: ToolCallCounter, name: str, *, gateway: bool = True) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use=_envelope(name, gateway=gateway, extra_input=None))
    counter.check(event)  # ty: ignore[invalid-argument-type]
    return event


def call_required(hook: RequirePriorCall, name: str, *, gateway: bool = True) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use=_envelope(name, gateway=gateway, extra_input=None))
    hook.check(event)  # ty: ignore[invalid-argument-type]
    return event


def test_tool_call_counter_allows_under_limit() -> None:
    counter = ToolCallCounter({"get_artifact": 3})
    for _ in range(3):
        event = call(counter, "get_artifact")
        assert event.cancel_tool is None


def test_tool_call_counter_denies_over_limit() -> None:
    counter = ToolCallCounter({"get_artifact": 3})
    for _ in range(3):
        call(counter, "get_artifact")
    fourth = call(counter, "get_artifact")
    assert fourth.cancel_tool is not None
    assert "cap of 3" in fourth.cancel_tool


def test_tool_call_counter_ignores_unlimited_tools() -> None:
    counter = ToolCallCounter({"get_artifact": 1})
    for _ in range(5):
        event = call(counter, "other_tool", gateway=False)
        assert event.cancel_tool is None


def test_tool_call_counter_resets_on_invocation() -> None:
    counter = ToolCallCounter({"get_artifact": 2})
    call(counter, "get_artifact")
    call(counter, "get_artifact")
    counter.reset(StubBeforeInvocation())  # ty: ignore[invalid-argument-type]
    after_reset = call(counter, "get_artifact")
    assert after_reset.cancel_tool is None


def test_require_prior_call_denies_target_before_prerequisite() -> None:
    hook = RequirePriorCall(target="write_memory_md", prerequisite="read_memory_md")
    event = call_required(hook, "write_memory_md")
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool


def test_require_prior_call_allows_target_after_prerequisite() -> None:
    hook = RequirePriorCall(target="write_memory_md", prerequisite="read_memory_md")
    call_required(hook, "read_memory_md")
    second = call_required(hook, "write_memory_md")
    assert second.cancel_tool is None


def test_require_prior_call_resets_on_invocation() -> None:
    hook = RequirePriorCall(target="write_memory_md", prerequisite="read_memory_md")
    call_required(hook, "read_memory_md")
    call_required(hook, "write_memory_md")
    hook.reset(StubBeforeInvocation())  # ty: ignore[invalid-argument-type]
    after_reset = call_required(hook, "write_memory_md")
    assert after_reset.cancel_tool is not None


def test_require_prior_call_ignores_unrelated_tools() -> None:
    hook = RequirePriorCall(target="write_memory_md", prerequisite="read_memory_md")
    event = call_required(hook, "search_codebase", gateway=False)
    assert event.cancel_tool is None


def call_multi(
    hook: RequireAllPriorCalls,
    name: str,
    *,
    gateway: bool = True,
) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use=_envelope(name, gateway=gateway, extra_input=None))
    hook.check(event)  # ty: ignore[invalid-argument-type]
    return event


def test_require_all_rejects_when_no_prerequisites_called() -> None:
    hook = RequireAllPriorCalls(
        target="put_artifact",
        prerequisites=["read_memory_md", "read_stack_profile_md"],
    )
    event = call_multi(hook, "put_artifact")
    assert event.cancel_tool is not None
    assert "`read_memory_md`" in event.cancel_tool
    assert "`read_stack_profile_md`" in event.cancel_tool


def test_require_all_rejects_when_some_prerequisites_missing() -> None:
    hook = RequireAllPriorCalls(
        target="put_artifact",
        prerequisites=["read_memory_md", "read_stack_profile_md"],
    )
    call_multi(hook, "read_memory_md")
    event = call_multi(hook, "put_artifact")
    assert event.cancel_tool is not None
    assert "`read_stack_profile_md`" in event.cancel_tool
    assert "`read_memory_md`" not in event.cancel_tool


def test_require_all_allows_after_every_prerequisite_called() -> None:
    hook = RequireAllPriorCalls(
        target="put_artifact",
        prerequisites=["read_memory_md", "read_stack_profile_md"],
    )
    call_multi(hook, "read_memory_md")
    call_multi(hook, "read_stack_profile_md")
    event = call_multi(hook, "put_artifact")
    assert event.cancel_tool is None


def test_require_all_resets_on_invocation() -> None:
    hook = RequireAllPriorCalls(target="put_artifact", prerequisites=["read_memory_md"])
    call_multi(hook, "read_memory_md")
    hook.reset(StubBeforeInvocation())  # ty: ignore[invalid-argument-type]
    after_reset = call_multi(hook, "put_artifact")
    assert after_reset.cancel_tool is not None


def test_require_all_rejects_empty_prerequisites_list() -> None:
    import pytest  # noqa: PLC0415

    with pytest.raises(ValueError, match="at least one prerequisite"):
        RequireAllPriorCalls(target="x", prerequisites=[])


def test_input_validator_passes_when_validator_returns_empty_list() -> None:
    hook = InputValidator(tool_names=("put_artifact",), validate=lambda _: [])
    event = StubBeforeToolCall(
        tool_use=_envelope("put_artifact", gateway=True, extra_input={"content": "..."}),
    )
    hook.check(event)  # ty: ignore[invalid-argument-type]
    assert event.cancel_tool is None


def test_input_validator_cancels_with_problems_joined() -> None:
    def validate(_: dict[str, Any]) -> list[str]:
        return ["missing Context section", "missing Approach section"]

    hook = InputValidator(tool_names=("put_artifact",), validate=validate)
    event = StubBeforeToolCall(
        tool_use=_envelope("put_artifact", gateway=True, extra_input={"content": "x"}),
    )
    hook.check(event)  # ty: ignore[invalid-argument-type]
    assert event.cancel_tool is not None
    assert "missing Context section" in event.cancel_tool
    assert "missing Approach section" in event.cancel_tool


def test_input_validator_ignores_unmatched_tools() -> None:
    def validate(_: dict[str, Any]) -> list[str]:
        return ["should not be called"]

    hook = InputValidator(tool_names=("put_artifact",), validate=validate)
    event = StubBeforeToolCall(
        tool_use=_envelope("get_artifact", gateway=True, extra_input={}),
    )
    hook.check(event)  # ty: ignore[invalid-argument-type]
    assert event.cancel_tool is None


def test_input_validator_handles_non_dict_input() -> None:
    """Defensive: tool input shapes vary; non-dict must not crash."""

    def validate(_: dict[str, Any]) -> list[str]:
        return ["should not be called"]

    hook = InputValidator(tool_names=("comment_pr",), validate=validate)
    event = StubBeforeToolCall(tool_use={"name": "comment_pr", "input": "not a dict"})
    hook.check(event)  # ty: ignore[invalid-argument-type]
    assert event.cancel_tool is None


def test_input_validator_rejects_empty_tool_names() -> None:
    import pytest  # noqa: PLC0415

    with pytest.raises(ValueError, match="at least one tool name"):
        InputValidator(tool_names=(), validate=lambda _: [])


def test_input_validator_supports_multiple_tool_names() -> None:
    def validate(_: dict[str, Any]) -> list[str]:
        return ["nope"]

    hook = InputValidator(tool_names=("comment_pr", "comment_issue"), validate=validate)
    for tool in ("comment_pr", "comment_issue"):
        event = StubBeforeToolCall(tool_use={"name": tool, "input": {"body": "x"}})
        hook.check(event)  # ty: ignore[invalid-argument-type]
        assert event.cancel_tool is not None


def test_effective_tool_name_returns_op_for_gateway_composite() -> None:
    tool_use = {"name": GATEWAY_NAME, "input": {"op": "put_artifact", "key": "x"}}
    assert effective_tool_name(tool_use) == "put_artifact"


def test_effective_tool_name_falls_back_to_composite_when_op_missing() -> None:
    tool_use = {"name": GATEWAY_NAME, "input": {"key": "x"}}
    assert effective_tool_name(tool_use) == GATEWAY_NAME


def test_effective_tool_name_falls_back_to_composite_when_op_not_string() -> None:
    tool_use = {"name": GATEWAY_NAME, "input": {"op": ""}}
    assert effective_tool_name(tool_use) == GATEWAY_NAME


def test_effective_tool_name_falls_back_to_composite_when_input_not_mapping() -> None:
    tool_use = {"name": GATEWAY_NAME, "input": "not a dict"}
    assert effective_tool_name(tool_use) == GATEWAY_NAME


def test_effective_tool_name_returns_plain_name_for_local_tool() -> None:
    tool_use = {"name": "browse_url", "input": {"url": "https://example.com"}}
    assert effective_tool_name(tool_use) == "browse_url"


def test_require_all_prior_calls_fires_on_real_gateway_envelope() -> None:
    """Direct repro of the bug in issue #81 — pre-fix this returned None."""
    hook = RequireAllPriorCalls(
        target="put_artifact",
        prerequisites=["read_memory_md", "read_stack_profile_md"],
    )
    event = StubBeforeToolCall(
        tool_use={"name": GATEWAY_NAME, "input": {"op": "put_artifact"}},
    )
    hook.check(event)  # ty: ignore[invalid-argument-type]
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool
