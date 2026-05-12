"""Deterministic lint/typecheck gate for the Implementer.

Runs ``uv run ruff check .`` and ``uv run ty check .`` as subprocesses
against the repo working tree. Returns a :class:`LintGateResult` with
structured pass/fail information. Neither check has network access —
both are purely local to the cloned workspace.

When a tool is absent (``FileNotFoundError``), the corresponding check is
skipped and ``gate_pass`` remains ``True`` for that tool unless the other
tool fails.  A subprocess timeout (30 s per tool) is treated as failure.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger()

_MAX_OUTPUT = 4096
_TIMEOUT = 30  # seconds per subprocess


@dataclass(frozen=True, slots=True)
class LintGateResult:
    """Structured outcome of one lint+type gate run.

    ``ruff_exit_code`` / ``ty_exit_code`` are ``None`` when the
    corresponding tool is not installed (skipped, not failed).
    ``ruff_output`` / ``ty_output`` are truncated to 4096 chars.
    ``gate_pass`` is ``True`` only when every installed tool exited 0.
    """

    gate_pass: bool
    ruff_exit_code: int | None
    ty_exit_code: int | None
    ruff_output: str
    ty_output: str

    @property
    def error_summary(self) -> str:
        """Combined diagnostic output for the lint-fix retry prompt."""
        parts: list[str] = []
        if self.ruff_exit_code not in (None, 0) and self.ruff_output:
            parts.append(f"## ruff check\n\n{self.ruff_output}")
        if self.ty_exit_code not in (None, 0) and self.ty_output:
            parts.append(f"## ty check\n\n{self.ty_output}")
        return "\n\n".join(parts)


def run_lint_gate(cwd: Path) -> LintGateResult:
    """Run ruff check and ty check in *cwd*; return a :class:`LintGateResult`.

    Args:
        cwd: Working directory — should be the repo checkout root.

    Returns:
        A frozen :class:`LintGateResult` describing the gate outcome.
    """
    ruff_code, ruff_out = _run_check(["uv", "run", "ruff", "check", "."], cwd=cwd, label="ruff")
    ty_code, ty_out = _run_check(["uv", "run", "ty", "check", "."], cwd=cwd, label="ty")

    gate_pass = (ruff_code is None or ruff_code == 0) and (ty_code is None or ty_code == 0)
    result = LintGateResult(
        gate_pass=gate_pass,
        ruff_exit_code=ruff_code,
        ty_exit_code=ty_code,
        ruff_output=ruff_out,
        ty_output=ty_out,
    )
    logger.info(
        "lint gate complete",
        gate_pass=gate_pass,
        ruff_exit_code=ruff_code,
        ty_exit_code=ty_code,
        ruff_output=ruff_out[:200] if ruff_out else "",
        ty_output=ty_out[:200] if ty_out else "",
    )
    return result


def _run_check(
    cmd: list[str],
    *,
    cwd: Path,
    label: str,
) -> tuple[int | None, str]:
    """Run one lint subprocess; return (exit_code, output).

    Returns ``(None, "")`` when the tool binary is absent (AC-009).
    Returns ``(1, message)`` on timeout.

    Args:
        cmd: Command list passed to :func:`subprocess.run`.
        cwd: Working directory for the subprocess.
        label: Short name used in log messages (``"ruff"`` / ``"ty"``).

    Returns:
        ``(exit_code, truncated_output)`` — exit code is ``None`` when
        the tool is not installed.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("lint gate: tool not found, skipping", tool=label, cmd=cmd[0])
        return None, ""
    except subprocess.TimeoutExpired:
        msg = f"{label} check timed out after {_TIMEOUT}s"
        logger.warning("lint gate: subprocess timeout", tool=label)
        return 1, msg[:_MAX_OUTPUT]

    output = (proc.stdout + proc.stderr).strip()
    truncated = output[:_MAX_OUTPUT]
    return proc.returncode, truncated
