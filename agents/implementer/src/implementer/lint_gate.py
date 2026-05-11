"""Deterministic lint/format/type/test gate for the Implementer.

Runs four Makefile targets sequentially after the agent completes editing.
On failure, the combined error output is returned so the caller can feed it
back to the agent as a follow-up message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class CommandResult(BaseModel):
    """Per-command result from one ``make <target>`` invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    output: Annotated[str, Field(max_length=4096)]


class LintGateResult(BaseModel):
    """Aggregate result of the four-command lint/format/type/test gate.

    ``passed`` is ``True`` only when all four commands exit with code 0.
    ``retry_count`` is 0 on the first pass and 1 after the agent has been
    given one round of corrective feedback.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    commands: list[CommandResult]
    retry_count: Annotated[int, Field(ge=0, le=1)]


_TARGETS = ("lint", "format", "type", "test")
_TIMEOUT = 60
_MAX_OUTPUT = 4096


def run_lint_gate(repo_root: Path, *, retry_count: int = 0) -> LintGateResult:
    """Run make lint/format/type/test sequentially in *repo_root*.

    All four commands always run (no early exit on first failure) so the
    agent gets complete feedback in a single pass.

    Args:
        repo_root: Absolute path to the repository root (where the Makefile
            lives).
        retry_count: 0 on the first pass, 1 on the retry.  Stored in the
            result for observability.

    Returns:
        A :class:`LintGateResult` with per-command results and an overall
        ``passed`` flag.
    """
    results: list[CommandResult] = []
    for target in _TARGETS:
        cmd = ["make", target]
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
            )
            raw = (proc.stdout + proc.stderr).strip()
            results.append(
                CommandResult(
                    command=f"make {target}",
                    exit_code=proc.returncode,
                    output=raw[:_MAX_OUTPUT],
                )
            )
        except subprocess.TimeoutExpired:
            results.append(
                CommandResult(
                    command=f"make {target}",
                    exit_code=1,
                    output=f"make {target} timed out after {_TIMEOUT}s"[:_MAX_OUTPUT],
                )
            )

    passed = all(r.exit_code == 0 for r in results)
    return LintGateResult(passed=passed, commands=results, retry_count=retry_count)


def compose_lint_feedback(gate_result: LintGateResult) -> str:
    """Build the follow-up user message from a failed :class:`LintGateResult`.

    Only failed commands are included to keep the context window tight.
    """
    lines = [
        "The lint/format/type/test gate failed. Fix the issues below, then call finish again.",
        "",
    ]
    for cmd in gate_result.commands:
        if cmd.exit_code != 0:
            lines += [
                f"## `{cmd.command}` (exit {cmd.exit_code})",
                "",
                cmd.output or "(no output)",
                "",
            ]
    return "\n".join(lines)
