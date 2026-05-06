"""Helpers that ground the Architect in the actual project repo.

The architect needs to read the project's tech stack before drafting
requirements/design — otherwise it hallucinates generic setups (Next.js
for a FastAPI project). On invocation the entrypoint shallow-clones the
target repo into ``/workspace/repo``; ``read_repo_file`` and
``list_repo_paths`` expose bounded views of that tree as Strands tools.

This module is read-only with respect to the target repo: no commits,
no pushes, no remote tracking — the architect's job is to look, not to
edit. Cleanup of the workspace happens implicitly when the AgentCore
microVM is recycled.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import structlog

from common.github_app import token_for_call

logger = structlog.get_logger()

WORKSPACE_ROOT = Path(os.environ.get("AIDLC_WORKSPACE_ROOT", "/workspace"))
REPO_PATH = WORKSPACE_ROOT / "repo"
GIT_BIN = "/usr/bin/git"
MAX_FILE_BYTES = 16384  # 16 KiB cap so tool output stays in context.
MAX_LIST_ENTRIES = 200  # ``list_repo_paths`` upper bound on returned paths.


def clone_target_repo(
    target_repo: str | None,
    *,
    requestor_sub: str | None = None,
) -> Path | None:
    """Shallow-clone ``target_repo`` into ``/workspace/repo``.

    Returns the cloned path, or ``None`` if no repo is configured (the
    architect runs without grounding — the prompt is explicit that
    listing/reading tools may return empty in that case).

    On AgentCore microVM reuse the workspace may already exist from a
    prior invocation; we rotate the access token and hard-reset to the
    latest default branch rather than re-cloning from scratch.
    """
    if not target_repo:
        return None
    target = REPO_PATH
    if target.exists():
        return refresh_clone(target, target_repo, requestor_sub=requestor_sub)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = token_for_call(repo=target_repo, requestor_sub=requestor_sub)
    url = f"https://x-access-token:{token}@github.com/{target_repo}.git"
    subprocess.run(  # noqa: S603 - args are well-formed
        [GIT_BIN, "clone", "--depth", "1", url, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("architect cloned target repo", target_repo=target_repo, path=str(target))
    return target


def refresh_clone(
    target: Path,
    target_repo: str,
    *,
    requestor_sub: str | None,
) -> Path:
    """Reuse an existing checkout: rotate the token, fetch, hard-reset."""
    token = token_for_call(repo=target_repo, requestor_sub=requestor_sub)
    url = f"https://x-access-token:{token}@github.com/{target_repo}.git"
    git("remote", "set-url", "origin", url, cwd=target)
    git("fetch", "--depth", "1", "origin", "HEAD", cwd=target)
    git("reset", "--hard", "FETCH_HEAD", cwd=target)
    git("clean", "-fdx", cwd=target)
    return target


def git(*args: str, cwd: Path) -> str:
    """Run a git command in ``cwd`` and return stdout."""
    proc = subprocess.run(  # noqa: S603 - args are well-formed
        [GIT_BIN, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def list_repo_paths(prefix: str = "", max_entries: int = MAX_LIST_ENTRIES) -> list[str]:
    """List tracked files in the cloned repo, optionally filtered by prefix.

    Args:
        prefix: Path prefix to filter on (empty string returns everything).
        max_entries: Hard cap on the returned list to keep tool output bounded.

    Returns:
        Sorted, deduplicated list of repo-relative paths. Empty when no
        repo is cloned (e.g. ``target_repo`` was unset for this run).
    """
    if not REPO_PATH.exists():
        return []
    output = git("ls-files", cwd=REPO_PATH)
    paths = [line for line in output.splitlines() if line]
    if prefix:
        paths = [p for p in paths if p.startswith(prefix)]
    cap = min(max_entries, MAX_LIST_ENTRIES)
    return paths[:cap]


def read_repo_file(path: str) -> str:
    """Read ``path`` from the cloned repo, capped at 16 KiB.

    Args:
        path: Repo-relative path (e.g. ``services/dashboard/pyproject.toml``).
            Absolute paths and any segment that escapes the workspace are
            rejected — the tool only exposes the project tree.

    Returns:
        UTF-8 file contents (invalid bytes replaced), or an empty string
        when the file is missing, the repo isn't cloned, or the path
        resolves outside the workspace.
    """
    if not REPO_PATH.exists():
        return ""
    candidate = REPO_PATH / path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return ""
    repo_real = REPO_PATH.resolve(strict=True)
    if not str(resolved).startswith(str(repo_real) + os.sep) and resolved != repo_real:
        return ""
    if not resolved.is_file():
        return ""
    return resolved.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
