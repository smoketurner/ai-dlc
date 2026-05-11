"""Deterministic lint/format/type/test gate for the Implementer.

Runs ``make lint``, ``make format``, ``make type``, and ``make test``
sequentially against the repo working tree. Returns a :class:`LintGateResult`
carrying per-command exit codes and truncated output so the caller can feed
failures back to the agent or record them for observability.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_COMMANDS: tuple[str, ...] = ("make lint", "make format", "make type", "make test")
_TIMEOUT_SECS: int = 60
_MAX_OUTPUT: int = 4096


class CommandResult(BaseModel):
    """Outcome of a single ``make <target>`` invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    output: Annotated[str, Field(max_length=_MAX_OUTPUT)]


class LintGateResult(BaseModel):
    """Aggregate result of running all four make targets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    commands: list[CommandResult]
    retry_count: Annotated[int, Field(ge=0, le=1)]


def run_lint_gate(path: Path, *, retry_count: int = 0) -> LintGateResult:
    """Run all four make targets against *path* and return the aggregate result.

    Each command runs with ``cwd=path``, a 60-second timeout, and combined
    stdout+stderr captured and truncated to 4096 characters. A timeout is
    treated as exit code 1 so downstream callers see a failure uniformly.
    """
    results: list[CommandResult] = []
    for cmd in _COMMANDS:
        result = _run_one(cmd, cwd=path)
        results.append(result)

    passed = all(r.exit_code == 0 for r in results)
    return LintGateResult(passed=passed, commands=results, retry_count=retry_count)


def _run_one(cmd: str, *, cwd: Path) -> CommandResult:
    args = cmd.split()
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
            check=False,
        )
        combined = (proc.stdout + proc.stderr)[:_MAX_OUTPUT]
        return CommandResult(command=cmd, exit_code=proc.returncode, output=combined)
    except subprocess.TimeoutExpired:
        return CommandResult(command=cmd, exit_code=1, output="timed out after 60s")
