"""Deterministic lint/typecheck/test/format gate for the Implementer.

After the Claude Agent SDK session finishes its edits, and before pushing the
branch, this module runs the four Makefile targets sequentially.  On failure
it feeds the error output back to the same agent session (via ``drive_agent``)
so the agent can self-remediate.  After up to ``MAX_REMEDIATION_PASSES``
attempts, it returns a ``LintGateResult`` that the caller uses to decide
whether to proceed or raise ``RuntimeError``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

from implementer.repo_ops import commit_changes, has_uncommitted_changes, repo_path

logger = structlog.get_logger()

MAX_REMEDIATION_PASSES = 3
GATE_COMMANDS = ("make test", "make lint", "make type", "make format")

_REMEDIATION_PROMPT_TEMPLATE = """\
The lint/type/test/format gate failed during the post-implementation check.

Command: {command}
Exit code: non-zero
Output:
{output}

Fix the issue above, then stop.  Do not open a PR or call ``finish`` — \
the gate will be re-run automatically after your fix.
"""


class DriveAgentFn(Protocol):
    """Signature expected by ``run_lint_gate`` for the remediation callback."""

    async def __call__(self, user_prompt: str, *, run_id: str) -> Any:
        """Drive the agent with ``user_prompt`` in the context of ``run_id``."""
        ...


@dataclass(frozen=True)
class LintGateResult:
    """Outcome of running the lint/type/test/format gate."""

    passed: bool
    attempts: int
    last_failure: str | None


def run_make_command(command: str, cwd: Path) -> tuple[int, str]:
    """Run a single make command and return ``(returncode, combined_output)``."""
    proc = subprocess.run(  # noqa: S602
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


def run_all_gate_commands(cwd: Path) -> tuple[bool, str, str]:
    """Run all four gate commands sequentially.

    Returns ``(all_passed, first_failing_command, first_failure_output)``.
    Stops on the first non-zero exit rather than running remaining commands.
    """
    for command in GATE_COMMANDS:
        returncode, output = run_make_command(command, cwd)
        if returncode != 0:
            logger.warning("gate command failed", command=command, returncode=returncode)
            return False, command, output
    return True, "", ""


async def run_lint_gate(
    *,
    run_id: str,
    drive_agent_fn: DriveAgentFn,
) -> LintGateResult:
    """Run the gate loop, calling ``drive_agent_fn`` on failures to self-remediate.

    Each attempt:
    1. Run all four gate commands.
    2. If all pass, return success.
    3. Compose a remediation prompt and call ``drive_agent_fn``.
    4. Commit any fixes the agent made.
    5. Repeat up to ``MAX_REMEDIATION_PASSES`` times.

    Returns a ``LintGateResult`` — the caller raises ``RuntimeError`` if
    ``passed is False``.
    """
    cwd = repo_path()
    last_failure: str | None = None

    for attempt in range(1, MAX_REMEDIATION_PASSES + 1):
        passed, failing_command, failure_output = run_all_gate_commands(cwd)
        if passed:
            logger.info("lint gate passed", run_id=run_id, attempt=attempt)
            return LintGateResult(passed=True, attempts=attempt, last_failure=None)

        last_failure = failure_output
        logger.warning(
            "lint gate failed",
            run_id=run_id,
            attempt=attempt,
            command=failing_command,
        )

        if attempt == MAX_REMEDIATION_PASSES:
            # Exhausted — don't call the agent again; fall through to return failure.
            break

        remediation_prompt = _REMEDIATION_PROMPT_TEMPLATE.format(
            command=failing_command,
            output=failure_output[:8000],
        )
        await drive_agent_fn(remediation_prompt, run_id=run_id)

        if has_uncommitted_changes():
            commit_changes(f"lint-gate fix (pass {attempt}): {failing_command}")

    return LintGateResult(
        passed=False,
        attempts=MAX_REMEDIATION_PASSES,
        last_failure=last_failure,
    )
