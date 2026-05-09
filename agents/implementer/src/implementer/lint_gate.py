"""Deterministic lint and type-check gate for the Implementer agent.

Runs three commands sequentially (ruff check, ruff format --check, ty check)
from the repo root using the workspace's ``uv run`` prefix. On failure the
caller can feed the error output back to the agent for one in-process retry
before proceeding to commit.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from common.runtime import CommandResult, LintGateResult

_TIMEOUT = 30  # seconds per command
_COMMANDS: tuple[str, ...] = (
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run ty check",
)
_OUTPUT_LIMIT = 4096


def run_lint_gate(repo_root: Path, *, retry_count: int = 0) -> LintGateResult:
    """Run all three lint/type commands and return an aggregated result.

    Each command is capped at ``_TIMEOUT`` seconds. A timeout is treated as
    a failure — the gate records exit_code=-1 and proceeds. All three
    commands always run so the agent gets complete feedback in one shot.

    Args:
        repo_root: Absolute path to the repository root (where pyproject.toml
            lives and where ``uv run`` resolves the workspace).
        retry_count: 0 on the first attempt, 1 after a retry.

    Returns:
        :class:`~common.runtime.LintGateResult` with one
        :class:`~common.runtime.CommandResult` per command.
    """
    results = []
    for cmd in _COMMANDS:
        result = _run_one(cmd, cwd=repo_root)
        results.append(result)

    passed = all(r.exit_code == 0 for r in results)
    return LintGateResult(passed=passed, commands=results, retry_count=retry_count)


def compose_lint_feedback(gate: LintGateResult) -> str:
    """Format a ``LintGateResult`` into a follow-up user message for the agent.

    Called when the gate failed on the first pass; the returned string is
    handed directly to ``drive_agent`` as the next user message so the agent
    can address the issues in a second pass.

    Args:
        gate: The failing :class:`~common.runtime.LintGateResult`.

    Returns:
        A Markdown-formatted prompt containing each failing command's output.
    """
    parts = [
        "The lint/type-check gate failed. Fix the issues below and call finish again.",
        "",
    ]
    for cmd_result in gate.commands:
        if cmd_result.exit_code != 0:
            parts += [
                f"## `{cmd_result.command}`",
                "",
                "```",
                cmd_result.output.rstrip(),
                "```",
                "",
            ]
    return "\n".join(parts)


def _run_one(cmd: str, *, cwd: Path) -> CommandResult:
    """Execute a single shell command and capture its output."""
    try:
        proc = subprocess.run(  # noqa: S603
            shlex.split(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
        output = (proc.stdout + proc.stderr)[:_OUTPUT_LIMIT]
        return CommandResult(command=cmd, exit_code=proc.returncode, output=output)
    except subprocess.TimeoutExpired:
        return CommandResult(command=cmd, exit_code=-1, output=f"timed out after {_TIMEOUT}s")
