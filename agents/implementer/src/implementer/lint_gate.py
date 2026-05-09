"""Deterministic lint/type-check gate for the Implementer.

Runs three commands sequentially from a given working directory:

  1. ``uv run ruff check .``
  2. ``uv run ruff format --check .``
  3. ``uv run ty check``

Returns a :class:`LintGateResult` with per-command exit codes and truncated
combined stdout+stderr (≤4096 chars per command) so callers can feed the
output back to the agent without blowing up the context window.

Timeouts (30 s per command) are treated as failures: the gate records
exit_code=-1 and proceeds — the outer caller decides whether to retry.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_MAX_OUTPUT = 4096
_TIMEOUT = 30

_COMMANDS: list[str] = [
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run ty check",
]


class CommandResult(BaseModel):
    """Per-command outcome from the lint gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    output: Annotated[str, Field(max_length=_MAX_OUTPUT)]


class LintGateResult(BaseModel):
    """Aggregate result returned by :func:`run_lint_gate`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    commands: list[CommandResult]
    retry_count: Annotated[int, Field(ge=0, le=1)]


def run_lint_gate(path: Path, *, retry_count: int = 0) -> LintGateResult:
    """Run all three lint/type commands under ``path`` and return a result.

    Args:
        path: Repo root to use as ``cwd`` for each subprocess.
        retry_count: 0 on the first pass, 1 on the retry; stored for observability.

    Returns:
        A :class:`LintGateResult` with per-command results and an aggregate
        ``passed`` flag (True only when every command exits with code 0).
    """
    results: list[CommandResult] = []
    for cmd in _COMMANDS:
        results.append(_run_command(cmd, cwd=path))

    passed = all(r.exit_code == 0 for r in results)
    return LintGateResult(passed=passed, commands=results, retry_count=retry_count)


def _run_command(cmd: str, *, cwd: Path) -> CommandResult:
    args = cmd.split()
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return CommandResult(
            command=cmd,
            exit_code=proc.returncode,
            output=combined[:_MAX_OUTPUT],
        )
    except subprocess.TimeoutExpired:
        return CommandResult(command=cmd, exit_code=-1, output="[timeout]")
