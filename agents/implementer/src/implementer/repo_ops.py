"""Git/GitHub operations for the Implementer.

The implementer needs to:

  * resolve the GitHub identity it'll act under for this run — either the
    requestor's user OAuth token (commits attribute to the user) or the
    App's installation token (commits attribute to ``ai-dlc[bot]``),
  * clone the target repo into ``/workspace/repo`` using that token,
  * fetch the architect's ``plan.md`` and the critic's ``critique.md``
    from S3 into ``/workspace/spec/`` via the per-agent gateway's
    ``artifact_tool`` target,
  * configure local git author from the resolved identity,
  * create the run's single impl branch ``aidlc/impl/{run_id}``,
  * let Claude edit, commit, push, and (post-agent) open one PR via the
    gateway's ``repo_helper`` target.

Git operations against the working tree stay on the container's
``git`` CLI — the agent loop iteratively edits + commits, and routing
each commit through a Lambda would batch-defer commits and break the
iterative model. GitHub API operations (open_pr, comment_pr, etc.)
flow through the gateway via :func:`invoke_repo_helper`.

The :class:`RepoSession` holds the token + author identity + target repo
for one Implementer invocation. ``execute_implementation`` builds the
session at the start of a run and passes it through every git
operation — no module-level env-var reads in this code path.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
from strands.tools.mcp import MCPClient

from common.gateway_tools import ARTIFACT_TOOL, REPO_HELPER, call_gateway_tool, extract_envelope
from common.github_app import (
    installation_token_for_repo,
    user_oauth_token_for_requestor_sub,
)

logger = structlog.get_logger()

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


def workspace_root() -> Path:
    """Container path under which the per-session checkout lives."""
    return Path(os.environ.get("AIDLC_WORKSPACE_ROOT", "/workspace"))


def repo_path() -> Path:
    """Project repo checkout path inside the container."""
    return workspace_root() / "repo"


def spec_path() -> Path:
    """Local plan/critique path inside the container."""
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
    email = f"{user_id}+{login}@users.noreply.github.com"
    return login, email


def run_git(*args: str, cwd: Path | None = None) -> str:
    """Run a git command and return stdout (raises on non-zero exit)."""
    cmd = [GIT_BIN, *args]
    proc = subprocess.run(  # noqa: S603 - args are well-formed
        cmd,
        cwd=cwd or repo_path(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = (
            f"git {shell_safe_join(list(args))} failed (exit {proc.returncode}) "
            f"cwd={cwd or repo_path()} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        raise RuntimeError(msg)
    return proc.stdout


def clone_repo(session: RepoSession, *, branch: str = "main") -> None:
    """Clone (or reset) the project repo in the container workspace.

    AgentCore Runtime reuses the microVM filesystem across invocations
    that share a ``runtime_session_id``. On reuse we always reset back
    to the base branch and update the access token before handing the
    repo to Claude Code.

    Uses ``--filter=blob:none`` (partial clone) so blob contents are
    fetched on demand rather than upfront.
    """
    target = repo_path()
    url = f"https://x-access-token:{session.access_token}@github.com/{session.target_repo}.git"
    if target.exists():
        reset_existing_clone(target, url=url, branch=branch)
        configure_git_author(session)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - args are well-formed
        [GIT_BIN, "clone", "--filter=blob:none", "--branch", branch, url, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    configure_git_author(session)


def reset_existing_clone(target: Path, *, url: str, branch: str) -> None:
    """Bring a reused workspace back to a clean ``branch`` checkout."""
    run_git("remote", "set-url", "origin", url, cwd=target)
    run_git("fetch", "origin", branch, cwd=target)
    run_git("checkout", "--detach", f"origin/{branch}", cwd=target)
    run_git("reset", "--hard", f"origin/{branch}", cwd=target)
    run_git("clean", "-fdx", cwd=target)


def configure_git_author(session: RepoSession) -> None:
    """Set ``user.name`` and ``user.email`` on the freshly-cloned repo."""
    run_git("config", "user.name", session.author_login)
    run_git("config", "user.email", session.author_email)


def fetch_plan_and_critique(
    mcp_client: MCPClient,
    *,
    plan_s3_key: str | None,
    critique_s3_key: str | None,
) -> None:
    """Download the plan + critique markdown bodies via the gateway into ``/workspace/spec/``.

    Best-effort: a missing artifact is logged but does not raise — the
    implementer can still proceed when only one of the artifacts is
    present (e.g., a re-run before the critic finished).
    """
    target = spec_path()
    target.mkdir(parents=True, exist_ok=True)
    for name, key in (("plan", plan_s3_key), ("critique", critique_s3_key)):
        if not key:
            continue
        try:
            envelope = call_artifact_tool(
                mcp_client,
                op="get_artifact",
                key=key,
            )
            body = str((envelope.get("result") or {}).get("content") or "")
        except Exception as exc:
            logger.warning(
                "fetch_plan_and_critique missed object",
                name=name,
                key=key,
                error=str(exc),
            )
            continue
        if not body:
            logger.warning("fetch_plan_and_critique got empty body", name=name, key=key)
            continue
        (target / f"{name}.md").write_text(body, encoding="utf-8")


def impl_branch_name(run_id: str) -> str:
    """Conventional branch name for the run's single impl PR.

    One impl branch per run. Mirrors
    ``state_router.actions.impl_branch_name`` so the wrapper and the
    router agree on the branch name without sharing imports.
    """
    return f"aidlc/impl/{run_id}"


def create_branch(branch: str) -> None:
    """Create + check out a fresh branch off the current HEAD.

    Uses ``-B`` (capital) so a leftover branch of the same name from a
    prior session is force-recreated rather than failing.
    """
    run_git("checkout", "-B", branch)


def checkout_impl_branch(branch: str) -> None:
    """Fetch + check out the run's impl branch as the working branch.

    Called in revision mode after ``clone_repo`` (which lands on
    ``main``). The impl branch already exists on origin from the
    initial implementation pass.
    """
    run_git("fetch", "origin", branch)
    run_git("checkout", "-B", branch, f"origin/{branch}")


def commit_changes(message: str) -> str:
    """Stage every change in the repo + create a commit. Return the SHA."""
    run_git("add", "-A")
    run_git("commit", "-m", message)
    return run_git("rev-parse", "HEAD").strip()


def push_branch(branch: str) -> None:
    """Push ``branch`` to ``origin``. Single-writer: plain push suffices."""
    run_git("push", "--set-upstream", "origin", branch)


def has_uncommitted_changes() -> bool:
    """``True`` when the working tree has any uncommitted modifications."""
    return bool(run_git("status", "--porcelain").strip())


def repo_made_real_changes() -> bool:
    """``True`` when commits or uncommitted edits exist on top of main.

    Compares ``HEAD`` against ``origin/main`` so this catches both edits
    the agent committed during its session and edits still in the
    working tree.
    """
    porcelain = run_git("status", "--porcelain").rstrip().splitlines()
    if porcelain:
        return True
    diff = run_git("diff", "--name-only", "origin/main...HEAD").strip()
    return bool(diff)


def short_diff_summary() -> str:
    """Return ``git diff --stat`` for the most recent commit."""
    return run_git("show", "--stat", "--format=", "HEAD")


def shell_safe_join(args: list[str]) -> str:
    """Re-join ``args`` for log lines without breaking shell quoting."""
    return " ".join(shlex.quote(a) for a in args)


# ----------------------------------------------------------------------
# Gateway-routed repo_helper / artifact_tool invocation
# ----------------------------------------------------------------------

PR_NUMBER_PATTERN = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)$")


def parse_pr_number(pr_url: str) -> int:
    """Extract the PR number from a github.com pull URL.

    Raises ``ValueError`` if the URL doesn't match the expected shape.
    """
    match = PR_NUMBER_PATTERN.match(pr_url)
    if match is None:
        msg = f"unparseable pr_url: {pr_url!r}"
        raise ValueError(msg)
    return int(match.group(1))


def invoke_repo_helper(
    mcp_client: MCPClient,
    *,
    op: str,
    requestor_sub: str | None,
    **fields: Any,
) -> dict[str, Any]:
    """Invoke the gateway-routed ``repo_helper`` target and return ``result`` dict.

    Raises ``RuntimeError`` on an ``ok: false`` envelope. The returned
    value is the Lambda's inner ``result`` payload (not the full
    envelope), matching the previous direct-Lambda contract that
    callers depend on.
    """
    response = call_gateway_tool(
        mcp_client,
        name=REPO_HELPER,
        arguments={"op": op, "requestor_sub": requestor_sub, **fields},
    )
    envelope = extract_envelope(response)
    if not envelope.get("ok"):
        msg = f"repo_helper.{op} failed: {envelope.get('error')}"
        raise RuntimeError(msg)
    result = envelope.get("result")
    return result if isinstance(result, dict) else {}


def call_artifact_tool(
    mcp_client: MCPClient,
    *,
    op: str,
    **fields: Any,
) -> dict[str, Any]:
    """Invoke the gateway-routed ``artifact_tool`` target and return the full envelope.

    Returns the Lambda's envelope (``{"ok": True, "op": ..., "result":
    {...}}``); callers extract the inner ``result`` themselves so
    they can distinguish ``result.content == ""`` from a missing key.
    Raises ``RuntimeError`` on an ``ok: false`` envelope.
    """
    response = call_gateway_tool(
        mcp_client,
        name=ARTIFACT_TOOL,
        arguments={"op": op, **fields},
    )
    envelope = extract_envelope(response)
    if not envelope.get("ok"):
        msg = f"artifact_tool.{op} failed: {envelope.get('error')}"
        raise RuntimeError(msg)
    return envelope


def post_inline_replies(
    mcp_client: MCPClient,
    *,
    repo: str,
    pr_number: int,
    requestor_sub: str | None,
    replies: list[tuple[int, str]],
) -> None:
    """Post a batch of PR-review-thread replies, one repo_helper call per reply."""
    for comment_id, body in replies:
        try:
            invoke_repo_helper(
                mcp_client,
                op="reply_pr_review_comment",
                requestor_sub=requestor_sub,
                repo=repo,
                pr_number=pr_number,
                comment_id=comment_id,
                body=body,
            )
        except Exception as exc:
            logger.warning(
                "reply_pr_review_comment failed",
                repo=repo,
                pr_number=pr_number,
                comment_id=comment_id,
                error=str(exc),
            )


def fetch_failed_check_runs(
    mcp_client: MCPClient,
    *,
    repo: str,
    head_sha: str,
    requestor_sub: str | None,
) -> list[dict[str, Any]]:
    """Return only the failing check runs for ``head_sha`` — for prompt context."""
    result = invoke_repo_helper(
        mcp_client,
        op="list_check_runs",
        requestor_sub=requestor_sub,
        repo=repo,
        ref=head_sha,
        filter_conclusions=["failure", "timed_out", "cancelled", "action_required", "stale"],
    )
    runs = result.get("check_runs", [])
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]
