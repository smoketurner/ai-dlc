"""Tests for ``implementer.hooks`` — finish validator + deny lists + audit log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk.types import HookContext, HookInput

from implementer.hooks import (
    audit_log_writes,
    deny_dangerous_bash,
    deny_sensitive_writes,
    validate_finish_report,
)


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


def bash_input(command: str) -> HookInput:
    return cast(
        "HookInput",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
    )


def write_input(file_path: str, *, tool: str = "Write") -> HookInput:
    return cast(
        "HookInput",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"file_path": file_path, "content": "x"},
        },
    )


def post_input(tool_name: str, tool_input: dict[str, Any]) -> HookInput:
    return cast(
        "HookInput",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "foo && rm -rf /etc",
        "rm -rf $HOME/work",
        "rm -rf ~/something",
        "chmod -R 777 /",
        "git push --force origin main",
        "git push --force-with-lease origin main",
        "pytest --no-verify",
        "aws iam list-users",
        "terraform apply -auto-approve",
        "kubectl delete pod x",
        "dropdb production",
        "psql -c 'DROP TABLE users'",
        "gh pr create --title x",
    ],
)
async def test_deny_dangerous_bash_blocks_known_patterns(command: str) -> None:
    result = await deny_dangerous_bash(bash_input(command), None, stub_context())
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "ls -lah /tmp",
        "git push origin feature/x",
        "rm /tmp/scratch.txt",  # not -rf
        "aws sts get-caller-identity",  # aws but not iam
        "uv run pytest",
        "ruff check .",
    ],
)
async def test_deny_dangerous_bash_allows_safe_commands(command: str) -> None:
    result = await deny_dangerous_bash(bash_input(command), None, stub_context())
    assert result == {}


@pytest.mark.asyncio
async def test_deny_dangerous_bash_only_fires_on_bash_tool() -> None:
    raw = cast(
        "HookInput",
        {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "/x"}},
    )
    result = await deny_dangerous_bash(raw, None, stub_context())
    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "app/.env",
        "config/secrets/key.pem",
        "secrets/api_key.txt",
        "credentials.json",
        "app/credentials/aws.json",
        "id_rsa",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".git-credentials",
    ],
)
async def test_deny_sensitive_writes_blocks_secret_paths(path: str) -> None:
    result = await deny_sensitive_writes(write_input(path), None, stub_context())
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        ".env.example",  # template, allowed
        ".env.local.template",  # also allowed
        "app/credentials_view.py",  # word-boundary: this is a Python module
        "docs/secrets-handling.md",  # documentation about secrets
        "tests/test_credentials_helper.py",
    ],
)
async def test_deny_sensitive_writes_allows_safe_paths(path: str) -> None:
    result = await deny_sensitive_writes(write_input(path), None, stub_context())
    assert result == {}


@pytest.mark.asyncio
async def test_deny_sensitive_writes_only_fires_on_write_or_edit() -> None:
    raw = cast(
        "HookInput",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
        },
    )
    result = await deny_sensitive_writes(raw, None, stub_context())
    assert result == {}


@pytest.mark.asyncio
async def test_audit_log_appends_one_jsonl_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AIDLC_AUDIT_LOG_PATH", str(log))
    write_event = post_input("Write", {"file_path": "/x", "content": "hi"})
    bash_event = post_input("Bash", {"command": "ls"})
    await audit_log_writes(write_event, None, stub_context())
    await audit_log_writes(bash_event, None, stub_context())
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["tool_name"] == "Write"
    assert lines[1]["tool_input"]["command"] == "ls"
    assert all("ts" in row for row in lines)


@pytest.mark.asyncio
async def test_audit_log_skips_non_mutating_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AIDLC_AUDIT_LOG_PATH", str(log))
    await audit_log_writes(post_input("Read", {"file_path": "/x"}), None, stub_context())
    await audit_log_writes(post_input("Glob", {"pattern": "**"}), None, stub_context())
    assert not log.exists()


@pytest.mark.asyncio
async def test_audit_log_swallows_io_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken audit log path must not break the agent."""

    def raise_oserror(*_: Any, **__: Any) -> None:
        msg = "simulated disk full"
        raise OSError(msg)

    monkeypatch.setattr(Path, "mkdir", raise_oserror)
    result = await audit_log_writes(
        post_input("Write", {"file_path": "/x", "content": "y"}),
        None,
        stub_context(),
    )
    assert result == {}
