"""Lint/type/format/test gate that runs before every branch push.

After the Claude Agent SDK session commits its work, ``run_verification_gate``
runs four ``make`` commands in the repo checkout directory.  On any failure
the gate feeds the output back to a new ``drive_agent`` session for a
remediation pass (up to ``MAX_GATE_ATTEMPTS`` total gate attempts).
If all attempts fail, it writes a ``BLOCKED.md`` artifact to S3 and raises
``RuntimeError``.

Each remediation pass opens a new ``ClaudeSDKClient`` session (the SDK's
async context manager pattern closes sessions after each ``drive_agent``
call).  The remediation prompt carries the failing command output so the
agent has enough context to fix the issue without the prior conversation
history.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from implementer.repo_ops import (
    call_artifact_tool,
    commit_changes,
    has_uncommitted_changes,
    repo_path,
)

if TYPE_CHECKING:
    from strands.tools.mcp import MCPClient

logger = structlog.get_logger()

MAKE_BIN = "/usr/bin/make"

# (make_target, timeout_seconds) — timeouts prevent hangs in `make test`.
GATE_COMMANDS: list[tuple[str, int]] = [
    ("test", 300),
    ("lint", 60),
    ("type", 60),
    ("format", 60),
]
# Number of gate *attempts* total (remediation passes = MAX_GATE_ATTEMPTS - 1).
MAX_GATE_ATTEMPTS = 3
# Maximum chars of command output sent to the remediation prompt.
_OUTPUT_TAIL = 4000


def _target_exists(target: str, cwd: Path) -> bool:
    """Return True when the Makefile in ``cwd`` defines ``target``.

    Uses ``make -n`` (dry-run) so nothing executes.  A missing Makefile,
    an undefined target (exit 2), or a preflight timeout all return False
    so the caller skips that gate rather than triggering a remediation
    loop for a missing make rule.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [MAKE_BIN, "-n", target],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired, OSError:
        return False
    else:
        return proc.returncode == 0


def run_make_command(
    target: str,
    cwd: Path,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run ``make <target>`` in ``cwd`` and return the result (no raise)."""
    return subprocess.run(  # noqa: S603
        [MAKE_BIN, target],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def run_all_gates(cwd: Path) -> tuple[bool, str, str]:
    """Run each gate command in sequence; stop at the first failure.

    Skips any target whose ``make`` rule does not exist in ``cwd`` — this
    keeps the gate safe for target repos that don't expose all four targets.

    Returns:
        ``(all_passed, failed_target, combined_output)`` where
        ``failed_target`` and ``combined_output`` are empty strings when all
        defined targets pass.
    """
    for target, timeout in GATE_COMMANDS:
        if not _target_exists(target, cwd):
            logger.warning(
                "gate: make target not defined, skipping",
                target=target,
                cwd=str(cwd),
            )
            continue
        try:
            proc = run_make_command(target, cwd, timeout=timeout)
        except subprocess.TimeoutExpired:
            combined = f"make {target} timed out after {timeout}s"
            logger.warning("gate: command timed out", target=target, timeout=timeout)
            return False, target, combined
        if proc.returncode != 0:
            combined = (proc.stdout + proc.stderr)[-_OUTPUT_TAIL:]
            logger.warning(
                "gate: command failed",
                target=target,
                returncode=proc.returncode,
            )
            return False, target, combined
        logger.info("gate: command passed", target=target)
    return True, "", ""


def compose_remediation_prompt(
    failed_target: str,
    output: str,
    pass_number: int,
) -> str:
    """Build the user message for a remediation agent session.

    Args:
        failed_target: The ``make`` target that exited non-zero.
        output:        Tail of combined stdout+stderr from the failing run.
        pass_number:   1-based attempt counter shown to the agent.
    """
    return (
        f"The following make target failed (attempt {pass_number}/{MAX_GATE_ATTEMPTS}). "
        f"Fix the issues and commit so the command exits 0:\n\n"
        f"$ make {failed_target}\n"
        f"{output}\n\n"
        f"Fix all errors so `make {failed_target}` exits 0. "
        "Commit your changes when done, then call `finish` with status=done."
    )


async def _remediate(run_id: str, failed_target: str, output: str, attempt: int) -> None:
    """Run one remediation drive_agent pass; swallow SDK/network errors."""
    # Lazy import breaks the client ↔ gates circular dependency.
    from implementer import client as _client  # noqa: PLC0415

    prompt = compose_remediation_prompt(failed_target, output, attempt)
    try:
        await _client.drive_agent(prompt, run_id=run_id)
    except Exception as exc:
        # SDK/network errors during remediation count as a failed attempt;
        # the loop will either retry or exhaust and raise.
        logger.warning(
            "gate: drive_agent raised during remediation",
            attempt=attempt,
            error=str(exc),
        )


async def run_verification_gate(
    run_id: str,
    *,
    mcp_client: MCPClient | None = None,
) -> None:
    """Run lint/type/format/test gates; remediate via agent on failure.

    Each pass runs all four ``make`` commands.  On failure, a new
    ``drive_agent`` session is opened with the error output as context.
    After ``MAX_GATE_ATTEMPTS`` failed attempts, writes ``BLOCKED.md``
    to S3 via the gateway (best-effort) and raises ``RuntimeError``.

    Args:
        run_id:     The current run identifier (used in BLOCKED.md key).
        mcp_client: Gateway MCP client for writing ``BLOCKED.md``; if
                    ``None`` the artifact write is skipped (local dev).
    """
    cwd = repo_path()
    failed_target = ""
    output = ""

    for attempt in range(1, MAX_GATE_ATTEMPTS + 1):
        passed, failed_target, output = run_all_gates(cwd)
        if passed:
            if has_uncommitted_changes():
                commit_changes("style: auto-format via make format")
            return

        logger.warning(
            "gate: attempt failed",
            attempt=attempt,
            max=MAX_GATE_ATTEMPTS,
            failed_target=failed_target,
        )
        if attempt < MAX_GATE_ATTEMPTS:
            await _remediate(run_id, failed_target, output, attempt)
            if has_uncommitted_changes():
                commit_changes(f"fix: gate remediation pass {attempt}")

    _write_blocked_md(run_id, mcp_client, failed_target, output)
    msg = f"verification gate failed after {MAX_GATE_ATTEMPTS} passes: make {failed_target}"
    raise RuntimeError(msg)


def _write_blocked_md(
    run_id: str,
    mcp_client: MCPClient | None,
    failed_target: str,
    output: str,
) -> None:
    """Write ``BLOCKED.md`` to S3 (best-effort; skipped when no mcp_client)."""
    if mcp_client is None:
        return
    content = (
        f"# Gate blocked\n\n"
        f"Run: {run_id}\n\n"
        f"After {MAX_GATE_ATTEMPTS} remediation attempts, "
        f"`make {failed_target}` still exits non-zero.\n\n"
        f"## Last output\n\n```\n{output}\n```\n"
    )
    try:
        call_artifact_tool(
            mcp_client,
            op="put_artifact",
            key=f"runs/{run_id}/BLOCKED.md",
            content=content,
        )
    except Exception as exc:
        logger.warning("gate: failed to write BLOCKED.md", run_id=run_id, error=str(exc))
