"""Helpers that ground the Architect in the actual project repo.

The architect needs to read the project's tech stack before drafting
requirements/design — otherwise it hallucinates generic setups (Next.js
for a FastAPI project). On invocation the entrypoint shallow-clones the
target repo into ``/workspace/repo``; ``read_repo_file`` and
``list_repo_paths`` expose bounded views of that tree as Strands tools.

After the clone, :func:`sync_memory_md_from_clone` syncs ``MEMORY.md``
(at the repo root, or under ``docs/`` for legacy projects) and
``AGENTS.md`` from the clone into the per-project
S3 bucket so downstream agents (Reviewer, Tester, Code-Critic,
Proposer) — which never clone — can ground themselves via
``read_memory_md``.

This module is read-only with respect to the target repo: no commits,
no pushes, no remote tracking — the architect's job is to look, not to
edit. Cleanup of the workspace happens implicitly when the AgentCore
microVM is recycled.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import structlog
from botocore.exceptions import ClientError

from common.github_app import token_for_call
from common.memory_md import write_stack_profile
from common.stack_discovery import discover_stack

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = structlog.get_logger()

WORKSPACE_ROOT = Path(os.environ.get("AIDLC_WORKSPACE_ROOT", "/workspace"))
REPO_PATH = WORKSPACE_ROOT / "repo"
GIT_BIN = "/usr/bin/git"
MAX_FILE_BYTES = 16384  # 16 KiB cap so tool output stays in context.
MAX_LIST_ENTRIES = 200  # ``list_repo_paths`` upper bound on returned paths.

MEMORY_MD_SOURCE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("MEMORY.md", "docs/MEMORY.md"),
    ("AGENTS.md",),
)
MEMORY_MD_KEY_TEMPLATE = "projects/{project_slug}/MEMORY.md"


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


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client for the memory_md bucket sync."""
    return boto3.client("s3")


def sync_memory_md_from_clone(*, project_slug: str, target_repo: str | None = None) -> None:
    """Sync the project's ``MEMORY.md`` + ``AGENTS.md`` from the clone into S3.

    Builds a single combined Markdown body with one section per source
    file present in the clone, then uploads to
    ``s3://{memory_md_bucket}/projects/{project_slug}/MEMORY.md`` —
    where every agent's ``read_memory_md`` looks. Idempotent on
    identical content (compared via the existing object's ETag, which
    is the body's MD5 for non-multipart uploads).

    No-op when neither source file exists in the clone (bucket stays
    empty so ``read_memory_md`` returns the empty string and the
    architect's grounding hook can fail closed). Sync failures are
    swallowed — the architect can still ground itself by calling
    ``read_repo_file`` against the clone directly.
    """
    bucket = os.environ.get("AIDLC_MEMORY_MD_BUCKET")
    if not bucket:
        logger.warning("AIDLC_MEMORY_MD_BUCKET unset; skipping MEMORY.md sync")
        return
    body = compose_memory_md_body(target_repo=target_repo)
    if body is None:
        logger.info(
            "no MEMORY.md sources in clone; skipping sync",
            project_slug=project_slug,
            source_groups=[list(group) for group in MEMORY_MD_SOURCE_GROUPS],
        )
        return
    key = MEMORY_MD_KEY_TEMPLATE.format(project_slug=project_slug)
    if existing_object_matches(bucket=bucket, key=key, body=body):
        logger.info(
            "MEMORY.md unchanged; skipping put",
            project_slug=project_slug,
            key=key,
        )
        return
    try:
        s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
    except ClientError as exc:
        logger.warning(
            "MEMORY.md sync failed",
            project_slug=project_slug,
            key=key,
            err=str(exc),
        )
        return
    logger.info("MEMORY.md synced", project_slug=project_slug, key=key, bytes=len(body))


def compose_memory_md_body(*, target_repo: str | None) -> str | None:
    """Read each MEMORY.md source group from the clone and join into one body.

    Each group is a tuple of candidate paths in preference order — root
    first, then ``docs/`` fallback. The first path that exists per group
    contributes one section to the body; subsequent paths in the same
    group are ignored so a repo that has both ``MEMORY.md`` and
    ``docs/MEMORY.md`` doesn't get duplicate content.

    Returns ``None`` when none of the sources exist — the caller treats
    that as "skip the sync entirely" so a project without grounding
    files doesn't get a placeholder S3 object that would mask the
    "fail closed" path in :func:`tools.read_memory_md`.

    The body intentionally embeds no timestamp: the S3 object's
    ``LastModified`` is the authoritative freshness signal and a
    body-level timestamp would defeat the MD5-based idempotency check
    in :func:`existing_object_matches`.
    """
    sections: list[str] = []
    for group in MEMORY_MD_SOURCE_GROUPS:
        for source in group:
            content = read_repo_file(source)
            if content:
                sections.append(f"## {source}\n\n{content.rstrip()}\n")
                break
    if not sections:
        return None
    header = ["# Project memory (synced from repo clone)", ""]
    if target_repo:
        header.append(f"> Source repo: {target_repo}")
        header.append("")
    return "\n".join(header) + "\n".join(sections)


def sync_stack_profile_from_clone(*, project_slug: str) -> None:
    """Walk the clone, run :func:`discover_stack`, and write the profile to S3.

    Idempotent on identical content (see
    :func:`common.memory_md.write_stack_profile`). Skipped silently when
    the workspace clone isn't present (no ``target_repo`` for this run);
    swallow any write error so a failed S3 put doesn't take down the
    architect run — the spec can still be drafted from
    ``list_repo_paths`` and ``read_repo_file``.

    Companion to :func:`sync_memory_md_from_clone`. Both functions write
    to the same per-project bucket so downstream agents (Reviewer,
    Tester, Code-Critic) pick up structured stack context alongside the
    human-written MEMORY.md.
    """
    if not REPO_PATH.exists():
        logger.info("no clone to scan; skipping stack profile sync", project_slug=project_slug)
        return
    try:
        profile = discover_stack(REPO_PATH)
        wrote = write_stack_profile(project_slug, profile)
    except (OSError, ClientError) as exc:
        logger.warning("stack profile sync failed", project_slug=project_slug, err=str(exc))
        return
    logger.info(
        "stack profile synced" if wrote else "stack profile unchanged; skipping put",
        project_slug=project_slug,
        components=len(profile.components),
        primary_language=profile.primary_language,
        polyglot=profile.polyglot,
    )


def existing_object_matches(*, bucket: str, key: str, body: str) -> bool:
    """Return True when the S3 object's ETag matches the candidate body's MD5.

    For non-multipart uploads ETag is the body's MD5 hex digest in
    quotes; we strip the quotes and compare. Any failure (object missing,
    permissions, multipart upload with a non-MD5 ETag) returns False so
    the caller falls through to put_object — better to over-write than
    silently skip a real change.
    """
    try:
        head = s3_client().head_object(Bucket=bucket, Key=key)
    except ClientError:
        return False
    etag = head.get("ETag", "").strip('"')
    if not etag:
        return False
    candidate_md5 = hashlib.md5(body.encode("utf-8"), usedforsecurity=False).hexdigest()
    return etag == candidate_md5
