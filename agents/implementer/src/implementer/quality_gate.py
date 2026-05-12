"""Deterministic lint/typecheck gate that runs after the agent's edit session.

:func:`run_gate` executes each :class:`GateCommand` via subprocess, collects
the combined stdout+stderr (truncated to 4096 chars), and returns a
:class:`GateOutcome` that the caller can use either to compose a retry prompt
(first failure) or a structured blocked_reason (second failure).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

_OUTPUT_LIMIT = 4096
_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class GateCommand:
    """One lint/typecheck command to run."""

    name: str
    command: str
    category: str  # "lint" | "format" | "typecheck"


@dataclass(frozen=True)
class GateResult:
    """Result of running one gate command."""

    command: GateCommand
    exit_code: int
    output: str  # combined stdout+stderr, truncated to _OUTPUT_LIMIT chars
    passed: bool


@dataclass(frozen=True)
class GateOutcome:
    """Aggregate result of all gate commands."""

    results: tuple[GateResult, ...]
    all_passed: bool
    retry_prompt: str | None  # composed only when all_passed is False
    blocked_reason: str | None  # composed only on second failure


def run_gate(commands: list[GateCommand], *, cwd: str) -> GateOutcome:
    """Execute every command and return the aggregate outcome.

    Args:
        commands: Ordered list of gate commands to run.
        cwd: Working directory for subprocess execution.

    Returns:
        A :class:`GateOutcome` with results for every command.
    """
    results = tuple(_run_one(cmd, cwd=cwd) for cmd in commands)
    all_passed = all(r.passed for r in results)
    if all_passed:
        return GateOutcome(
            results=results,
            all_passed=True,
            retry_prompt=None,
            blocked_reason=None,
        )
    return GateOutcome(
        results=results,
        all_passed=False,
        retry_prompt=_compose_retry_prompt(results),
        blocked_reason=_compose_blocked_reason(results),
    )


def _run_one(cmd: GateCommand, *, cwd: str) -> GateResult:
    """Run a single command, capturing combined stdout+stderr.

    Args:
        cmd: The gate command to execute.
        cwd: Working directory.

    Returns:
        A :class:`GateResult` with the exit code and truncated output.
    """
    try:
        proc = subprocess.run(  # noqa: S602
            cmd.command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
        combined = proc.stdout + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        # On timeout, treat as failure; compose partial output from what we have.
        raw_out = exc.stdout or b""
        raw_err = exc.stderr or b""
        stdout = raw_out.decode(errors="replace") if isinstance(raw_out, bytes) else raw_out
        stderr = raw_err.decode(errors="replace") if isinstance(raw_err, bytes) else raw_err
        combined = stdout + stderr + f"\n[command timed out after {_TIMEOUT_SECONDS}s]"
        exit_code = 1

    output = _truncate(combined)
    return GateResult(
        command=cmd,
        exit_code=exit_code,
        output=output,
        passed=exit_code == 0,
    )


def _truncate(text: str) -> str:
    """Return the last _OUTPUT_LIMIT characters of *text*.

    Args:
        text: Arbitrary string to truncate.

    Returns:
        ``text`` unchanged when ≤ _OUTPUT_LIMIT chars; the tail otherwise.
    """
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return text[-_OUTPUT_LIMIT:]


def _compose_retry_prompt(results: tuple[GateResult, ...]) -> str:
    """Build a retry prompt containing every failing command's output.

    Args:
        results: All gate results (mix of pass and fail).

    Returns:
        A multi-line string ready to send as a user message to the agent.
    """
    parts = [
        "One or more quality gate commands failed. Fix the violations and call "
        "finish again. Do not open a new branch or PR.\n",
    ]
    for r in results:
        if not r.passed:
            parts.append(
                f"## {r.command.name} ({r.command.category}) — exit {r.exit_code}\n"
                f"Command: {r.command.command}\n\n"
                f"Output (last {_OUTPUT_LIMIT} chars):\n```\n{r.output}\n```\n",
            )
    return "\n".join(parts)


def _compose_blocked_reason(results: tuple[GateResult, ...]) -> str:
    """Build a structured blocked_reason string for TASK.BLOCKED.

    Args:
        results: All gate results (mix of pass and fail).

    Returns:
        A compact string identifying every failing command and its output.
    """
    lines: list[str] = ["Quality gate failed after retry:"]
    for r in results:
        if not r.passed:
            lines.append(
                f"  [{r.command.name}] exit={r.exit_code} cmd={r.command.command!r} "
                f"output={r.output!r}"
            )
    return "\n".join(lines)
