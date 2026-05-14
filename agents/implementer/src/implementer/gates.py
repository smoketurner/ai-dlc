"""Post-agent lint/type/test gate for the implementer.

Runs ``make test``, ``make lint``, ``make type``, and ``make format``
in the cloned target repo.  Any target that is absent from the repo's
Makefile is skipped (treated as pass) so the gate is safe against
target repos that do not define every command.

All output is capped at :data:`MAX_OUTPUT_BYTES` before being embedded
in a remediation prompt — ``make test`` can produce megabytes of pytest
output that would blow the model's context window.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_OUTPUT_BYTES = 32 * 1024  # 32 KB per command
GATE_TIMEOUT_SECONDS = int(os.environ.get("AIDLC_GATE_TIMEOUT_SECONDS", "300"))
_MAKE_NO_RULE_EXIT_CODE = 2  # ``make -n <target>`` returns 2 when the target is absent

_GATE_COMMANDS = ("make test", "make lint", "make type", "make format")


@dataclass
class CommandResult:
    """Output from a single gate command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class GateResult:
    """Aggregated result of running all four gate commands."""

    passed: bool
    failures: list[CommandResult] = field(default_factory=list)
    all_results: list[CommandResult] = field(default_factory=list)


def run_lint_gates(cwd: Path, *, timeout: int = GATE_TIMEOUT_SECONDS) -> GateResult:
    """Run make test/lint/type/format in ``cwd``; skip missing targets.

    All four commands are attempted regardless of earlier failures so
    the agent receives the complete picture in one remediation pass.
    Returns a :class:`GateResult` with ``passed=True`` only when every
    present target exits 0.
    """
    all_results: list[CommandResult] = []
    failures: list[CommandResult] = []

    for cmd in _GATE_COMMANDS:
        target = cmd.split()[1]
        if not _target_exists(target, cwd=cwd, timeout=timeout):
            continue
        result = _run_command(cmd, cwd=cwd, timeout=timeout)
        all_results.append(result)
        if result.exit_code != 0:
            failures.append(result)

    return GateResult(passed=not failures, failures=failures, all_results=all_results)


def build_remediation_prompt(
    result: GateResult,
    *,
    format_committed: bool = False,
) -> str:
    """Format gate failures into a user message the agent can act on.

    Args:
        result: The failing :class:`GateResult`.
        format_committed: When ``True``, a note is added that the
            formatter has already written and committed its changes.
    """
    parts = [
        "The post-agent lint/type/test gate failed. Fix every issue "
        "listed below and call `finish` again.",
        "",
    ]
    for failure in result.failures:
        truncated = (
            len(failure.stdout) >= MAX_OUTPUT_BYTES or len(failure.stderr) >= MAX_OUTPUT_BYTES
        )
        parts.append(f"## `{failure.command}` failed (exit {failure.exit_code})")
        if failure.stdout:
            parts.append(f"**stdout:**\n```\n{failure.stdout}\n```")
        if failure.stderr:
            parts.append(f"**stderr:**\n```\n{failure.stderr}\n```")
        if truncated:
            parts.append(
                "_(Output truncated to 32 KB. Full output available "
                "in BLOCKED.md if the gate is exhausted.)_"
            )
        parts.append("")

    if format_committed:
        parts += [
            "**Note:** `make format` wrote formatting changes that have "
            "already been committed.  You do not need to re-run the "
            "formatter manually.",
            "",
        ]
    else:
        parts += [
            "**Note:** If `make format` wrote formatting changes to disk, "
            "commit them with "
            '`git add -A && git commit -m "style: apply ruff format"` '
            "before fixing other issues.",
            "",
        ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _target_exists(target: str, *, cwd: Path, timeout: int) -> bool:
    """Return ``True`` when the Makefile in ``cwd`` defines ``target``.

    Uses ``make -n <target>`` (dry-run); exit code 2 means "no rule for
    target" and any other code means the target exists (even if it would
    produce no output for up-to-date artefacts).
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["make", "-n", target],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired, FileNotFoundError:
        return False
    else:
        return result.returncode != _MAKE_NO_RULE_EXIT_CODE


def _run_command(cmd: str, *, cwd: Path, timeout: int) -> CommandResult:
    """Execute ``cmd`` via the shell; cap stdout/stderr to MAX_OUTPUT_BYTES."""
    try:
        proc = subprocess.run(  # noqa: S602
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return CommandResult(
            command=cmd,
            exit_code=proc.returncode,
            stdout=_tail(proc.stdout),
            stderr=_tail(proc.stderr),
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command=cmd,
            exit_code=-1,
            stdout="",
            stderr=f"command timed out after {timeout}s",
        )


def _tail(text: str) -> str:
    """Return the last MAX_OUTPUT_BYTES characters of ``text``."""
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    return text[-MAX_OUTPUT_BYTES:]
