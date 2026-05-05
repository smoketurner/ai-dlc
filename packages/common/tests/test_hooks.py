"""Tests for ``common.hooks``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.hooks import RequirePriorCall, ToolCallCounter, validate_no_spec_dump


@dataclass
class StubBeforeToolCall:
    """Minimal stand-in for ``strands.hooks.BeforeToolCallEvent``.

    Strands' hook helpers only read ``tool_use["name"]`` and write
    ``cancel_tool``, so a duck-typed dataclass is enough for unit tests.
    """

    tool_use: dict[str, Any]
    cancel_tool: str | None = None


@dataclass
class StubBeforeInvocation:
    """Minimal stand-in for ``BeforeInvocationEvent`` — payload unused."""

    agent: object = field(default=None)


def call(counter: ToolCallCounter, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    counter.check(event)  # type: ignore[arg-type]
    return event


def call_required(hook: RequirePriorCall, name: str) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name})
    hook.check(event)  # type: ignore[arg-type]
    return event


def test_validate_no_spec_dump_clean_text_returns_none() -> None:
    assert validate_no_spec_dump("This PR adds a `/healthz` endpoint.") is None


def test_validate_no_spec_dump_flags_requirements_heading() -> None:
    body = "## Summary\n\nstuff\n\n# Requirements\n\nfoo"
    reason = validate_no_spec_dump(body)
    assert reason is not None
    assert "Requirements" in reason


def test_validate_no_spec_dump_flags_tasks_md_filename() -> None:
    body = "Some text\n\n## tasks.md\n\ntext"
    reason = validate_no_spec_dump(body)
    assert reason is not None
    assert "tasks" in reason


def test_validate_no_spec_dump_does_not_flag_design_considerations() -> None:
    """`# Design considerations` is a normal heading, not a spec dump."""
    body = "## Design considerations\n\nWe chose Option A because..."
    assert validate_no_spec_dump(body) is None


def test_validate_no_spec_dump_handles_indented_headings() -> None:
    body = "  # Requirements\n"
    assert validate_no_spec_dump(body) is not None


def test_tool_call_counter_allows_under_limit() -> None:
    counter = ToolCallCounter({"read_spec_doc": 3})
    for _ in range(3):
        event = call(counter, "read_spec_doc")
        assert event.cancel_tool is None


def test_tool_call_counter_denies_over_limit() -> None:
    counter = ToolCallCounter({"read_spec_doc": 3})
    for _ in range(3):
        call(counter, "read_spec_doc")
    fourth = call(counter, "read_spec_doc")
    assert fourth.cancel_tool is not None
    assert "cap of 3" in fourth.cancel_tool


def test_tool_call_counter_ignores_unlimited_tools() -> None:
    counter = ToolCallCounter({"read_spec_doc": 1})
    for _ in range(5):
        event = call(counter, "other_tool")
        assert event.cancel_tool is None


def test_tool_call_counter_resets_on_invocation() -> None:
    counter = ToolCallCounter({"read_spec_doc": 2})
    call(counter, "read_spec_doc")
    call(counter, "read_spec_doc")
    counter.reset(StubBeforeInvocation())  # type: ignore[arg-type]
    after_reset = call(counter, "read_spec_doc")
    assert after_reset.cancel_tool is None


def test_require_prior_call_denies_target_before_prerequisite() -> None:
    hook = RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md")
    event = call_required(hook, "write_spec_doc")
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool


def test_require_prior_call_allows_target_after_prerequisite() -> None:
    hook = RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md")
    call_required(hook, "read_memory_md")
    second = call_required(hook, "write_spec_doc")
    assert second.cancel_tool is None


def test_require_prior_call_resets_on_invocation() -> None:
    hook = RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md")
    call_required(hook, "read_memory_md")
    call_required(hook, "write_spec_doc")
    hook.reset(StubBeforeInvocation())  # type: ignore[arg-type]
    after_reset = call_required(hook, "write_spec_doc")
    assert after_reset.cancel_tool is not None


def test_require_prior_call_ignores_unrelated_tools() -> None:
    hook = RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md")
    event = call_required(hook, "search_codebase")
    assert event.cancel_tool is None
