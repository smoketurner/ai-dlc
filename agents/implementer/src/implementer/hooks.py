"""Pre- and post-tool-use hooks enforcing the project's deny-list.

The Implementer runs in an AgentCore Runtime container with the project repo
checked out at ``/workspace/repo``. Claude Code's ``Bash`` tool can run any
shell command by default; these hooks block the dangerous ones called out in
the project's ``AGENTS.md`` and add per-tool guards for ``Write`` / ``Edit``
against secret files.

Patterns are compiled regex with word boundaries so a doc edit that mentions
"terraform apply" in prose doesn't trip the Bash deny-list, and writes to
``.env.example`` aren't denied by the ``.env`` rule. Compound commands
(``foo && rm -rf /``) are still caught because the regex matches anywhere in
the command string.

The ``finish`` tool gets a ``PostToolUse`` matcher (:func:`validate_finish_report`)
that re-validates the payload against :class:`implementer.finish.FinishReport`
and runs :func:`common.hooks.validate_no_spec_dump` against the summary so
Claude retries when it tries to dump the spec into the report.

The :func:`audit_log_writes` ``PostToolUse`` hook appends one JSONL row per
mutating tool call to ``/workspace/audit.jsonl`` for post-session forensics.

:func:`require_finish_on_stop` is a ``Stop`` hook factory: it blocks the
session from ending until the agent has actually called ``finish``. Without
it, the SDK happily lets Claude drop off the end of the loop with an empty
:class:`FinishSink`, leaving the wrapper with no structured report.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from aws_lambda_powertools import Logger
from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput
from pydantic import ValidationError

from common.hooks import validate_no_spec_dump
from common.steering import Accept, JudgeResult, Retry
from implementer.finish import FinishReport, FinishSink

logger = Logger(service="implementer")

DANGEROUS_BASH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-[a-z]*rf?[a-z]*\s+/", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*rf?[a-z]*\s+\$HOME\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*rf?[a-z]*\s+~", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+--force(?:-with-lease)?(?:\s|=).*\bmain\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)--no-verify\b", re.IGNORECASE),
    re.compile(r"\baws\s+iam\b", re.IGNORECASE),
    re.compile(r"\bterraform\s+apply\b", re.IGNORECASE),
    re.compile(r"\bkubectl\s+delete\b", re.IGNORECASE),
    re.compile(r"\bdropdb\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bgh\s+pr\s+create\b", re.IGNORECASE),  # PRs go through repo_ops.open_pr
)

SENSITIVE_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)\.env(?:$|/)"),  # .env or .env/ but not .env.example
    re.compile(r"(?:^|/)secrets?(?:$|/)", re.IGNORECASE),
    re.compile(r"(?:^|/)credentials?(?:$|/|\.json$)", re.IGNORECASE),
    re.compile(r"(?:^|/)id_(?:rsa|ed25519|ecdsa)(?:$|\.)"),
    re.compile(r"(?:^|/)\.aws/(?:credentials|config)$"),
    re.compile(r"(?:^|/)\.git-credentials$"),
)

AUDIT_LOG_PATH_ENV = "AIDLC_AUDIT_LOG_PATH"
DEFAULT_AUDIT_LOG_PATH = "/workspace/audit.jsonl"
MUTATING_TOOLS = ("Write", "Edit", "Bash", "NotebookEdit")


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
    """PreToolUse hook on ``Bash``: block destructive shell commands.

    Patterns are word-boundary regex so prose mentions (``echo "terraform
    apply"``) don't trip the deny — but they still match the actual
    command, even when chained with ``;``, ``&&``, ``||``, or ``|``.
    """
    raw = cast("dict[str, Any]", input_data)
    if raw.get("tool_name") != "Bash":
        return allow()
    command = str(raw.get("tool_input", {}).get("command", ""))
    for pattern in DANGEROUS_BASH_PATTERNS:
        if pattern.search(command):
            return deny(f"deny-list match: {pattern.pattern!r}")
    return allow()


async def deny_sensitive_writes(
    input_data: HookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> SyncHookJSONOutput:
    """PreToolUse hook on ``Write``/``Edit``: block edits to secrets/keys.

    Patterns are anchored so ``.env.example`` is allowed while ``.env``
    is denied; ``credentials.json`` is denied while ``credentials/`` as
    a directory name in unrelated context (``app/credentials_view.py``)
    is allowed.
    """
    raw = cast("dict[str, Any]", input_data)
    name = raw.get("tool_name")
    if name not in {"Write", "Edit"}:
        return allow()
    file_path = str(raw.get("tool_input", {}).get("file_path", ""))
    for pattern in SENSITIVE_PATH_PATTERNS:
        if pattern.search(file_path):
            return deny(f"refusing to write a sensitive path: {pattern.pattern!r}")
    return allow()


async def audit_log_writes(
    input_data: HookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> SyncHookJSONOutput:
    """PostToolUse hook: append a JSONL row for every mutating tool call.

    Writes one line per call to ``$AIDLC_AUDIT_LOG_PATH`` (default
    ``/workspace/audit.jsonl``). Failure to append never blocks the
    agent — audit-log issues should not interrupt work in flight.
    """
    raw = cast("dict[str, Any]", input_data)
    name = str(raw.get("tool_name") or "")
    if name not in MUTATING_TOOLS:
        return allow()
    record = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "tool_name": name,
        "tool_input": raw.get("tool_input", {}),
    }
    try:
        path = Path(os.environ.get(AUDIT_LOG_PATH_ENV, DEFAULT_AUDIT_LOG_PATH))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append is small (one JSONL row); the cost of going via asyncio.to_thread
        # outweighs the benefit for an audit-only side effect that must not block.
        with path.open("a", encoding="utf-8") as fh:  # noqa: ASYNC230
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass
    return allow()


def judge_finish_report(args: dict[str, Any]) -> JudgeResult:
    """Judge a ``finish`` payload — pure function, no SDK types.

    Catches two failure modes:

      * Malformed payloads that slipped past the tool's own
        ``model_validate`` (paranoid second line of defense).
      * Summaries that quote spec-document headings verbatim (spec leak
        heuristic — see :func:`common.hooks.validate_no_spec_dump`).

    Args:
        args: The ``tool_input`` dict the agent passed to ``finish``.

    Returns:
        :class:`Accept` when the report is well-formed and not a spec
        dump; :class:`Retry` (with an actionable reason) otherwise.
    """
    try:
        report = FinishReport.model_validate(args)
    except ValidationError as exc:
        return Retry(
            reason=f"FinishReport validation failed: {exc.errors(include_url=False)!r}",
        )
    leak_reason = validate_no_spec_dump(report.summary)
    if leak_reason is not None:
        return Retry(
            reason=(
                f"`finish` summary appears to dump spec content ({leak_reason}). "
                "Rewrite as one paragraph in your own words and call `finish` again."
            ),
        )
    return Accept()


async def validate_finish_report(
    input_data: HookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> SyncHookJSONOutput:
    """PostToolUse hook on ``finish``: adapt :func:`judge_finish_report` to the SDK.

    The pure judgement lives in :func:`judge_finish_report` so it can be
    tested without the Claude Agent SDK types. This wrapper converts a
    :class:`Retry` verdict into ``permissionDecision="deny"`` (which
    prompts Claude to retry with the reason as guidance).
    """
    raw = cast("dict[str, Any]", input_data)
    args = raw.get("tool_input", {})
    verdict = judge_finish_report(args)
    if isinstance(verdict, Retry):
        return deny_post(verdict.reason)
    return allow()


HookCallback = Callable[
    [HookInput, str | None, HookContext],
    Coroutine[Any, Any, SyncHookJSONOutput],
]

FINISH_REQUIRED_REASON = (
    "You did not call the `finish` tool. Call "
    "`mcp__finish_server__finish` now with `summary`, `files_changed`, "
    "`tests_run`, `risks`, and `status='done'` (or `status='blocked'` "
    "plus `blocked_reason` if you cannot proceed). The platform uses "
    "your finish report to open the PR — without it the PR ships with "
    "an empty body. Do not stop until finish has been called."
)


def block_stop(reason: str) -> SyncHookJSONOutput:
    """Return a Stop-hook output that tells Claude to keep going."""
    return cast("SyncHookJSONOutput", {"decision": "block", "reason": reason})


def require_finish_on_stop(sink: FinishSink) -> HookCallback:
    """Build a Stop hook that blocks shutdown until ``finish`` has fired.

    Returns an async callable matching the SDK's hook signature, closed
    over the per-session :class:`FinishSink`. Behaviour:

    * ``sink.report`` is set → allow the stop (the agent finished cleanly).
    * ``sink.report`` is ``None`` and ``stop_hook_active`` is ``False``
      (first stop attempt this session) → return ``decision='block'`` with
      :data:`FINISH_REQUIRED_REASON`. Claude resumes the loop and (per
      system prompt + reason) should call ``finish`` next.
    * ``sink.report`` is ``None`` and ``stop_hook_active`` is ``True``
      (we already blocked once and Claude is trying to stop again) →
      log a warning and allow the stop. Letting it loop further would
      just burn the turn / budget cap. The implementer wrapper still
      handles ``report=None`` gracefully (empty-summary fall-backs in
      the PR title/body).
    """

    async def stop_hook(
        input_data: HookInput,
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> SyncHookJSONOutput:
        if sink.report is not None:
            return allow()
        raw = cast("dict[str, Any]", input_data)
        if raw.get("stop_hook_active"):
            logger.warning(
                "agent stopped without calling finish even after a block; "
                "proceeding with report=None",
            )
            return allow()
        return block_stop(FINISH_REQUIRED_REASON)

    return stop_hook
