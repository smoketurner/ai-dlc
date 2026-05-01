"""Thin git CLI helpers for the Implementer agent's persistent filesystem.

The Implementer mutates files under ``/workspace`` and commits to a
session-scoped branch. We deliberately keep the surface minimal — branching,
adding, committing — and leave PR creation to the AgentCore Gateway's GitHub
target so the agent doesn't carry GitHub credentials directly.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from common.errors import GitOpError


def run(args: list[str], /, *, cwd: Path) -> str:
    """Run a git command and return stripped stdout, raising on failure.

    Raises:
        GitOpError: When git exits non-zero. The stderr is included in
            ``context`` for triage.
    """
    cmd = ["git", *args]
    try:
        completed = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitOpError("git binary not found", cwd=str(cwd)) from exc
    if completed.returncode != 0:
        raise GitOpError(
            "git command failed",
            cmd=shlex.join(["git", *args]),
            cwd=str(cwd),
            stderr=completed.stderr.strip(),
            returncode=completed.returncode,
        )
    return completed.stdout.strip()


def create_branch(branch: str, *, cwd: Path, base: str = "main") -> None:
    """Create and check out a fresh branch from ``base``."""
    run(["fetch", "origin", base], cwd=cwd)
    run(["checkout", "-B", branch, f"origin/{base}"], cwd=cwd)


def add_all(cwd: Path) -> None:
    """Stage every change in the working tree."""
    run(["add", "-A"], cwd=cwd)


def commit(message: str, *, cwd: Path, author_name: str, author_email: str) -> str:
    """Create a commit and return its SHA.

    The commit author identity is set explicitly per call rather than
    relying on git config, so a session never accidentally commits as the
    runtime container's default user.
    """
    identity = [
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
    ]
    run([*identity, "commit", "-m", message], cwd=cwd)
    return run(["rev-parse", "HEAD"], cwd=cwd)


def diff_summary(cwd: Path, /, *, base: str = "main") -> str:
    """Return a short ``--stat`` diff between the current branch and ``base``."""
    return run(["diff", "--stat", f"origin/{base}...HEAD"], cwd=cwd)
