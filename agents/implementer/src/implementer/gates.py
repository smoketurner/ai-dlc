"""Deterministic lint/type/test gate loop for the Implementer.

After the agent's first-pass work is committed, ``run_lint_gates`` runs
``make test``, ``make lint``, ``make type``, and ``make format`` in
sequence.  A passing pass (all four exit 0) allows execution to
continue toward ``push_branch``.  A failing pass feeds the combined
stdout+stderr back to the agent for a remediation commit, then retries.
After ``max_passes`` failed attempts, ``GatesBlockedError`` is raised so
``execute_implementation`` can write ``BLOCKED.md`` and let ``app.py``
emit ``RUN.FAILED``.

``make format`` is *mutating*: after a pass where all four commands
exit 0, any uncommitted changes it left (reformatted files) are
committed before returning success.
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from implementer.repo_ops import commit_changes, has_uncommitted_changes, repo_path

logger = structlog.get_logger()

_GATE_TARGETS = ("test", "lint", "type", "format")
_MAX_OUTPUT_BYTES = 8192

# Type alias for the injectable agent-calling callable.
DriveAgentFn = Callable[..., Awaitable[tuple[Any, dict[str, Any]]]]


class GatesBlockedError(Exception):
    """Raised when all remediation passes are exhausted.

    Carries the name of the last failing command and its combined output
    so ``execute_implementation`` can embed them in ``BLOCKED.md``.
    """

    def __init__(self, command: str, output: str) -> None:
        """Record the last failing command and its combined output."""
        super().__init__(f"gates blocked after exhausting passes: {command}")
        self.command = command
        self.output = output


def run_make_command(target: str) -> tuple[int, str]:
    """Run ``make <target>`` from the repo root and return (exit_code, output).

    stdout and stderr are merged into a single string.  The call never
    raises — a non-zero exit code is returned to the caller.
    """
    proc = subprocess.run(  # noqa: S603 - target is one of four known make targets
        ["make", target],  # noqa: S607 - relative make is correct here
        cwd=repo_path(),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


async def run_lint_gates(
    run_id: str,
    drive_agent_fn: DriveAgentFn,
    *,
    max_passes: int = 3,
) -> None:
    """Run the four make gates, driving the agent to remediate failures.

    On success (all four commands exit 0), any uncommitted formatting
    changes from ``make format`` are committed and the function returns.

    On exhaustion (``max_passes`` all fail), ``GatesBlockedError`` is
    raised carrying the last failing command name and its output.

    Exceptions from ``drive_agent_fn`` are not caught — they propagate
    immediately so the caller's exception handler (``app.py``) sees the
    SDK error rather than a misleading ``GatesBlockedError``.
    """
    if not (repo_path() / "Makefile").exists():
        logger.warning("gates: Makefile not found, skipping gate checks", run_id=run_id)
        return

    last_command = ""
    last_output = ""

    for pass_num in range(1, max_passes + 1):
        failed_command: str | None = None
        failed_output: str | None = None

        for target in _GATE_TARGETS:
            exit_code, output = run_make_command(target)
            logger.info(
                "gates: make target",
                run_id=run_id,
                target=target,
                pass_num=pass_num,
                exit_code=exit_code,
            )
            if exit_code != 0:
                failed_command = target
                failed_output = output
                last_command = target
                last_output = output
                break  # stop at the first failure in this pass

        if failed_command is None:
            # All four targets passed — commit any formatting side-effects and return.
            if has_uncommitted_changes():
                commit_changes(f"gates: apply formatting (pass {pass_num})")
                logger.info("gates: committed formatting changes", run_id=run_id, pass_num=pass_num)
            logger.info("gates: all targets passed", run_id=run_id, pass_num=pass_num)
            return

        # At least one target failed — call the agent for remediation.
        # Truncate from the tail: test/lint tools write the summary last.
        truncated_output = failed_output[-_MAX_OUTPUT_BYTES:] if failed_output else ""
        remediation_prompt = _build_remediation_prompt(
            run_id=run_id,
            command=failed_command,
            output=truncated_output,
            pass_num=pass_num,
        )
        logger.info(
            "gates: invoking agent for remediation",
            run_id=run_id,
            failed_command=failed_command,
            pass_num=pass_num,
        )
        # drive_agent_fn exceptions propagate intentionally — see module docstring.
        await drive_agent_fn(remediation_prompt, run_id=run_id)

        # Commit whatever the agent produced (including any formatting fixes).
        if has_uncommitted_changes():
            commit_changes(f"gates: remediation commit (pass {pass_num})")

    raise GatesBlockedError(last_command, last_output)


def _build_remediation_prompt(
    *,
    run_id: str,
    command: str,
    output: str,
    pass_num: int,
) -> str:
    """Compose the remediation prompt sent to the agent after a gate failure."""
    return "\n".join(
        [
            f"Run id: {run_id}",
            f"Gates pass {pass_num} failed on: make {command}",
            "",
            "The command produced the following output (last 8 KiB):",
            "```",
            output,
            "```",
            "",
            "Fix every issue reported above, then call `finish` when done.",
            "Keep changes minimal — address only what the failing command reports.",
        ]
    )
