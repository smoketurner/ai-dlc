"""Tests for ``architect.hooks``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from architect.hooks import (
    PLAN_SECTION_NAMES,
    build_hooks,
    validate_plan_artifact,
)


@dataclass
class StubBeforeToolCall:
    """Duck-typed stand-in for ``strands.hooks.BeforeToolCallEvent``."""

    tool_use: dict[str, Any]
    cancel_tool: str | None = None


def call(hook: Any, name: str, tool_input: dict[str, Any] | None = None) -> StubBeforeToolCall:
    event = StubBeforeToolCall(tool_use={"name": name, "input": tool_input or {}})
    hook.check(event)
    return event


def full_plan_body() -> str:
    """A plan body containing every required section heading at level 2."""
    return "\n\n".join(f"## {name}\n\nbody for {name}" for name in PLAN_SECTION_NAMES)


def test_build_hooks_returns_two_hooks() -> None:
    """One ``RequireAllPriorCalls`` + one ``InputValidator``."""
    assert len(build_hooks()) == 2


def test_put_artifact_blocked_when_no_prerequisites_called() -> None:
    require = build_hooks()[0]
    event = call(require, "put_artifact")
    assert event.cancel_tool is not None
    assert "read_memory_md" in event.cancel_tool
    assert "read_stack_profile_md" in event.cancel_tool


def test_put_artifact_blocked_when_only_one_prerequisite_called() -> None:
    require = build_hooks()[0]
    call(require, "read_memory_md")
    event = call(require, "put_artifact")
    assert event.cancel_tool is not None
    assert "read_stack_profile_md" in event.cancel_tool


def test_put_artifact_allowed_after_both_prerequisites_called() -> None:
    require = build_hooks()[0]
    call(require, "read_memory_md")
    call(require, "read_stack_profile_md")
    event = call(require, "put_artifact")
    assert event.cancel_tool is None


def test_each_invocation_gets_independent_hook_instances() -> None:
    """Two calls to build_hooks should not share state."""
    a = build_hooks()[0]
    b = build_hooks()[0]
    call(a, "read_memory_md")
    call(a, "read_stack_profile_md")
    event_b = call(b, "put_artifact")
    assert event_b.cancel_tool is not None


def test_validate_plan_artifact_passes_for_complete_body() -> None:
    tool_input = {"key": "runs/abc/plan.md", "content": full_plan_body()}
    assert validate_plan_artifact(tool_input) == []


def test_validate_plan_artifact_reports_missing_sections() -> None:
    body = "## Context\nfoo\n\n## Approach\nbar"
    tool_input = {"key": "runs/abc/plan.md", "content": body}
    problems = validate_plan_artifact(tool_input)
    assert problems
    assert "Assumptions" in problems[0]
    assert "Verification" in problems[0]


def test_validate_plan_artifact_skips_non_plan_keys() -> None:
    """Non-plan artifacts (ADRs, critiques, ...) bypass the section check."""
    tool_input = {"key": "runs/abc/critique.md", "content": "anything goes"}
    assert validate_plan_artifact(tool_input) == []


def test_input_validator_cancels_put_artifact_with_bad_plan() -> None:
    validator = build_hooks()[1]
    event = call(
        validator,
        "put_artifact",
        {"key": "runs/abc/plan.md", "content": "## Context only"},
    )
    assert event.cancel_tool is not None
    assert "missing required level-2 section" in event.cancel_tool


def test_input_validator_allows_complete_plan() -> None:
    validator = build_hooks()[1]
    event = call(
        validator,
        "put_artifact",
        {"key": "runs/abc/plan.md", "content": full_plan_body()},
    )
    assert event.cancel_tool is None
