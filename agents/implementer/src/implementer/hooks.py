"""Pre-tool-use hooks enforcing the project's deny-list.

The Implementer runs in an AgentCore Runtime container with the project repo
checked out at ``/workspace/repo``. Claude Code's ``Bash`` tool can run any
shell command by default; these hooks block the dangerous ones called out in
``CLAUDE.md`` and add per-tool guards for ``Write``/``Edit`` against secret
files.
"""

from __future__ import annotations

from typing import Any, cast

from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput

DANGEROUS_BASH_PATTERNS = (
    "rm -rf /",
    "rm -rf $HOME",
    "rm -rf ~",
    "chmod -R 777",
    "git push --force-with-lease origin main",
    "git push --force origin main",
    " --no-verify",
    "aws iam ",
    "terraform apply",
    "kubectl delete",
    "dropdb ",
    "DROP TABLE",
)

SENSITIVE_PATH_FRAGMENTS = (
    ".env",
    "secrets",
    "credentials",
    "id_rsa",
    "id_ed25519",
)


def deny(reason: str) -> SyncHookJSONOutput:
    """Return a PreToolUse JSON output that denies the tool call."""
    return cast(
        "SyncHookJSONOutput",
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        },
    )


def allow() -> SyncHookJSONOutput:
    """Return a PreToolUse JSON output that allows the tool call."""
    return cast("SyncHookJSONOutput", {})


async def deny_dangerous_bash(
    input_data: HookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> SyncHookJSONOutput:
    """PreToolUse hook on ``Bash``: block destructive shell commands."""
    raw = cast("dict[str, Any]", input_data)
    if raw.get("tool_name") != "Bash":
        return allow()
    command = str(raw.get("tool_input", {}).get("command", ""))
    for pattern in DANGEROUS_BASH_PATTERNS:
        if pattern in command:
            return deny(f"deny-list match: {pattern!r}")
    return allow()


async def deny_sensitive_writes(
    input_data: HookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> SyncHookJSONOutput:
    """PreToolUse hook on ``Write``/``Edit``: block edits to secrets/keys."""
    raw = cast("dict[str, Any]", input_data)
    name = raw.get("tool_name")
    if name not in {"Write", "Edit"}:
        return allow()
    file_path = str(raw.get("tool_input", {}).get("file_path", ""))
    for fragment in SENSITIVE_PATH_FRAGMENTS:
        if fragment in file_path:
            return deny(f"refusing to write a sensitive path: {fragment!r}")
    return allow()
