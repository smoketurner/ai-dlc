"""Tests for ``implementer.hooks`` — focused on ``validate_finish_report``.

The Bash + Write/Edit deny-list hooks are exercised indirectly today by
``test_tasks.py`` and the Implementer's container smoke; this file
focuses on the new ``finish`` PostToolUse validator.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from claude_agent_sdk.types import HookContext, HookInput

from implementer.hooks import validate_finish_report


def stub_context() -> HookContext:
    return cast("HookContext", {})


def hook_input(args: dict[str, Any]) -> HookInput:
    return cast(
        "HookInput",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__finish_server__finish",
            "tool_input": args,
        },
    )


@pytest.mark.asyncio
async def test_validate_finish_report_allows_clean_payload() -> None:
    args = {
        "summary": "Added /healthz endpoint.",
        "files_changed": ["app/main.py"],
        "tests_run": [{"name": "test_health", "status": "pass"}],
        "risks": [],
        "status": "done",
    }
    result = await validate_finish_report(hook_input(args), None, stub_context())
    assert result == {}  # `allow()` returns empty dict


@pytest.mark.asyncio
async def test_validate_finish_report_denies_invalid_payload() -> None:
    # blocked status with no blocked_reason — should fail Pydantic validation
    args = {"summary": "x", "status": "blocked"}
    result = await validate_finish_report(hook_input(args), None, stub_context())
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "validation failed" in output["permissionDecisionReason"].lower()


@pytest.mark.asyncio
async def test_validate_finish_report_denies_spec_dump() -> None:
    args = {
        "summary": "Did the work.\n\n# Requirements\n\nThe agent shall...",
        "files_changed": ["app/main.py"],
        "tests_run": [],
        "risks": [],
        "status": "done",
    }
    result = await validate_finish_report(hook_input(args), None, stub_context())
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "spec" in output["permissionDecisionReason"].lower()


@pytest.mark.asyncio
async def test_validate_finish_report_denies_tasks_md_leak() -> None:
    args = {
        "summary": "Did stuff.\n\n## tasks.md\n\nfoo",
        "status": "done",
    }
    result = await validate_finish_report(hook_input(args), None, stub_context())
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_validate_finish_report_allows_design_in_normal_text() -> None:
    """Words like 'design' inside normal prose must not trip the heuristic."""
    args = {
        "summary": "Refactored the design of the cache layer to use LRU.",
        "status": "done",
    }
    result = await validate_finish_report(hook_input(args), None, stub_context())
    assert result == {}
