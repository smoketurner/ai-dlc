"""Git/GitHub operations for the Implementer.

The implementer needs to:

  * clone the project repo into ``/workspace/repo``,
  * fetch the spec bundle from S3 into ``/workspace/spec/``,
  * create a new branch named after the task,
  * after Claude finishes editing, commit + push,
  * open a PR via the GitHub API.

Phase 6 scope: structurally complete code with subprocess-based ``git`` and
``httpx``-based GitHub API calls. The actual smoke run depends on real
GitHub credentials and a live AWS account, so end-to-end is deferred. Unit
tests cover the pure helpers; the I/O wrappers are integration-tested.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import httpx

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

GIT_BIN = "/usr/bin/git"


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding spec artifacts."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def github_token() -> str:
    """OAuth token used for GitHub API calls."""
    return os.environ["AIDLC_GITHUB_TOKEN"]


def github_repo() -> str:
    """Project's GitHub repo in ``owner/name`` form."""
    return os.environ["AIDLC_GITHUB_REPO"]


def workspace_root() -> Path:
    """Container path under which the per-session checkout lives."""
    return Path(os.environ.get("AIDLC_WORKSPACE_ROOT", "/workspace"))


def repo_path() -> Path:
    """Project repo checkout path inside the container."""
    return workspace_root() / "repo"


def spec_path() -> Path:
    """Local spec bundle path inside the container."""
    return workspace_root() / "spec"


def run_git(*args: str, cwd: Path | None = None) -> str:
    """Run a git command and return stdout (raises on non-zero exit)."""
    cmd = [GIT_BIN, *args]
    proc = subprocess.run(  # noqa: S603 - args are well-formed
        cmd,
        cwd=cwd or repo_path(),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def clone_repo(branch: str = "main") -> None:
    """Clone the project repo into the container workspace."""
    target = repo_path()
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{github_token()}@github.com/{github_repo()}.git"
    subprocess.run(  # noqa: S603 - args are well-formed
        [GIT_BIN, "clone", "--depth", "1", "--branch", branch, url, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


def fetch_spec(spec_s3_prefix: str) -> None:
    """Download the three spec docs from S3 into ``/workspace/spec/``."""
    target = spec_path()
    target.mkdir(parents=True, exist_ok=True)
    bucket = artifacts_bucket()
    for doc in ("requirements", "design", "tasks"):
        key = f"{spec_s3_prefix.rstrip('/')}/{doc}.md"
        body = s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
        (target / f"{doc}.md").write_bytes(body)


def task_branch_name(task_id: str, spec_slug: str) -> str:
    """Conventional branch name for a single-task PR."""
    return f"aidlc/{spec_slug}/{task_id.lower()}"


def create_branch(branch: str) -> None:
    """Create + check out a fresh branch off the current HEAD."""
    run_git("checkout", "-b", branch)


def commit_changes(message: str) -> str:
    """Stage every change in the repo + create a commit. Return the SHA."""
    run_git("add", "-A")
    run_git("commit", "-m", message)
    return run_git("rev-parse", "HEAD").strip()


def push_branch(branch: str) -> None:
    """Push ``branch`` to ``origin``."""
    run_git("push", "--set-upstream", "origin", branch)


def open_pr(*, branch: str, base: str, title: str, body: str) -> str:
    """Open a PR via the GitHub REST API; return the PR HTML URL."""
    url = f"https://api.github.com/repos/{github_repo()}/pulls"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "head": branch, "base": base}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["html_url"]


def short_diff_summary() -> str:
    """Return ``git diff --stat`` for the most recent commit."""
    return run_git("show", "--stat", "--format=", "HEAD")


def shell_safe_join(args: list[str]) -> str:
    """Re-join ``args`` for log lines without breaking shell quoting."""
    return " ".join(shlex.quote(a) for a in args)
