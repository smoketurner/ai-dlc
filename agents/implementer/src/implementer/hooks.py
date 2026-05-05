"""Pre- and post-tool-use hooks enforcing the project's deny-list.

The Implementer runs in an AgentCore Runtime container with the project repo
checked out at ``/workspace/repo``. Claude Code's ``Bash`` tool can run any
shell command by default; these hooks block the dangerous ones called out in
``CLAUDE.md`` and add per-tool guards for ``Write``/``Edit`` against secret
files.

The ``finish`` tool gets a ``PostToolUse`` matcher (:func:`validate_finish_report`)
that re-validates the payload against :class:`implementer.finish.FinishReport`
and runs :func:`common.hooks.validate_no_spec_dump` against the summary so
Claude retries when it tries to dump the spec into the report.
"""

from __future__ import annotations

from typing import Any, cast

from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput
from pydantic import ValidationError

from common.hooks import validate_no_spec_dump
from implementer.finish import FinishReport

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


def deny_post(reason: str) -> SyncHookJSONOutput:
    """Return a PostToolUse JSON output that asks Claude to retry."""
    return cast(
        "SyncHookJSONOutput",
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        },
    )


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


async def validate_finish_report(
    input_data: HookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> SyncHookJSONOutput:
    """PostToolUse hook on ``finish``: re-validate payload + reject spec dumps.

    The ``finish`` tool already validates with Pydantic, so this hook is a
    second line of defense — it catches:

      * malformed payloads that somehow slipped past the tool's
        ``model_validate`` (paranoid),
      * agent output where the ``summary`` quotes a spec heading verbatim
        (the spec-leak heuristic in :func:`common.hooks.validate_no_spec_dump`).

    On either failure the hook returns ``permissionDecision="deny"`` so
    Claude is prompted to retry with corrected input.
    """
    raw = cast("dict[str, Any]", input_data)
    args = raw.get("tool_input", {})
    try:
        report = FinishReport.model_validate(args)
    except ValidationError as exc:
        return deny_post(f"FinishReport validation failed: {exc.errors(include_url=False)!r}")
    leak_reason = validate_no_spec_dump(report.summary)
    if leak_reason is not None:
        return deny_post(
            f"`finish` summary appears to dump spec content ({leak_reason}). "
            "Rewrite as one paragraph in your own words and call `finish` again."
        )
    return allow()
