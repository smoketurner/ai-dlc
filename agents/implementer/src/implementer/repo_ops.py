"""Git/GitHub operations for the Implementer.

The implementer needs to:

  * resolve the GitHub identity it'll act under for this run — either the
    requestor's user OAuth token (commits attribute to the user) or the
    App's installation token (commits attribute to ``ai-dlc[bot]``),
  * clone the target repo into ``/workspace/repo`` using that token,
  * fetch the spec bundle from S3 into ``/workspace/spec/``,
  * configure local git author from the resolved identity,
  * create a new branch named after the task, let Claude edit, commit,
    push, and open a PR via the GitHub API.

The :class:`RepoSession` holds the token + author identity + target repo
for one Implementer invocation. ``execute_task`` builds the session at
the start of a run and passes it through every git/GitHub operation —
no module-level env-var reads in this code path.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import httpx

from implementer.agentcore_auth import (
    installation_token_for_repo,
    user_oauth_token_for_requestor_sub,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

GIT_BIN = "/usr/bin/git"
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
BOT_LOGIN_FALLBACK = "ai-dlc[bot]"
BOT_EMAIL_FALLBACK = "ai-dlc-bot@users.noreply.github.com"


@dataclass(frozen=True)
class RepoSession:
    """Per-invocation auth + identity context for git/GitHub calls."""

    target_repo: str  # owner/name
    access_token: str  # bearer (user OAuth or installation token)
    author_login: str  # git config user.name
    author_email: str  # git config user.email
    on_behalf_of_user: bool  # True when commits attribute to a real user


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding spec artifacts."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def workspace_root() -> Path:
    """Container path under which the per-session checkout lives."""
    return Path(os.environ.get("AIDLC_WORKSPACE_ROOT", "/workspace"))


def repo_path() -> Path:
    """Project repo checkout path inside the container."""
    return workspace_root() / "repo"


def spec_path() -> Path:
    """Local spec bundle path inside the container."""
    return workspace_root() / "spec"


def make_session(*, target_repo: str, requestor_sub: str | None) -> RepoSession:
    """Resolve auth + author identity for one Implementer run.

    When ``requestor_sub`` is set and the user has authorized the GitHub
    App, the session uses the user's OAuth token and queries
    ``GET /user`` to derive the git author identity. Otherwise falls back
    to the App's installation token and ``ai-dlc[bot]`` attribution.
    """
    if requestor_sub:
        token = user_oauth_token_for_requestor_sub(requestor_sub)
        if token is not None:
            login, email = resolve_user_identity(token)
            return RepoSession(
                target_repo=target_repo,
                access_token=token,
                author_login=login,
                author_email=email,
                on_behalf_of_user=True,
            )
    return RepoSession(
        target_repo=target_repo,
        access_token=installation_token_for_repo(target_repo),
        author_login=BOT_LOGIN_FALLBACK,
        author_email=BOT_EMAIL_FALLBACK,
        on_behalf_of_user=False,
    )


def resolve_user_identity(token: str) -> tuple[str, str]:
    """Look up the GitHub login + noreply email for ``token`` via GET /user."""
    response = httpx.get(
        f"{GITHUB_API}/user",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    login = str(body["login"])
    user_id = int(body["id"])
    # GitHub's privacy-respecting noreply email pattern. See
    # https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address
    email = f"{user_id}+{login}@users.noreply.github.com"
    return login, email


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


def clone_repo(session: RepoSession, *, branch: str = "main") -> None:
    """Clone (or reset) the project repo in the container workspace.

    AgentCore Runtime reuses the microVM filesystem across invocations
    that share a ``runtime_session_id``. On reuse the working tree may
    carry leftover branches, uncommitted edits, or the wrong HEAD from
    a prior run, so we always reset back to the configured base branch
    and update the access token before handing the repo to Claude Code.
    """
    target = repo_path()
    url = f"https://x-access-token:{session.access_token}@github.com/{session.target_repo}.git"
    if target.exists():
        reset_existing_clone(target, url=url, branch=branch)
        configure_git_author(session)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - args are well-formed
        [GIT_BIN, "clone", "--depth", "1", "--branch", branch, url, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    configure_git_author(session)


def reset_existing_clone(target: Path, *, url: str, branch: str) -> None:
    """Bring a reused workspace back to a clean ``branch`` checkout.

    Refreshes ``origin``'s URL (the access token is freshly minted each
    invocation), fetches the latest base branch, hard-resets HEAD onto
    it, and removes any untracked files. Idempotent.
    """
    run_git("remote", "set-url", "origin", url, cwd=target)
    run_git("fetch", "--depth", "1", "origin", branch, cwd=target)
    # Detach HEAD before resetting so any old task branch can be safely
    # force-recreated by ``create_branch`` later.
    run_git("checkout", "--detach", f"origin/{branch}", cwd=target)
    run_git("reset", "--hard", f"origin/{branch}", cwd=target)
    run_git("clean", "-fdx", cwd=target)


def configure_git_author(session: RepoSession) -> None:
    """Set ``user.name`` and ``user.email`` on the freshly-cloned repo."""
    run_git("config", "user.name", session.author_login)
    run_git("config", "user.email", session.author_email)


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
    """Create + check out a fresh branch off the current HEAD.

    Uses ``-B`` (capital) so a leftover branch of the same name from a
    prior session is force-recreated rather than failing with exit 128.
    """
    run_git("checkout", "-B", branch)


def commit_changes(message: str) -> str:
    """Stage every change in the repo + create a commit. Return the SHA."""
    run_git("add", "-A")
    run_git("commit", "-m", message)
    return run_git("rev-parse", "HEAD").strip()


def push_branch(branch: str) -> None:
    """Push ``branch`` to ``origin``."""
    run_git("push", "--set-upstream", "origin", branch)


def open_pr(session: RepoSession, *, branch: str, base: str, title: str, body: str) -> str:
    """Open a PR via the GitHub REST API; return the PR HTML URL."""
    url = f"{GITHUB_API}/repos/{session.target_repo}/pulls"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {session.access_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "head": branch, "base": base}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["html_url"]


def short_diff_summary() -> str:
    """Return ``git diff --stat`` for the most recent commit."""
    return run_git("show", "--stat", "--format=", "HEAD")


def shell_safe_join(args: list[str]) -> str:
    """Re-join ``args`` for log lines without breaking shell quoting."""
    return " ".join(shlex.quote(a) for a in args)
