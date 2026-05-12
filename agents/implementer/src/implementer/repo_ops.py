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

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import structlog

from common.github_app import (
    installation_token_for_repo,
    user_oauth_token_for_requestor_sub,
)

logger = structlog.get_logger()

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient
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
    that share a ``runtime_session_id``. On reuse the working tree may
    carry leftover branches, uncommitted edits, or the wrong HEAD from
    a prior run, so we always reset back to the configured base branch
    and update the access token before handing the repo to Claude Code.

    Uses ``--filter=blob:none`` (partial clone) so blob contents are
    fetched on demand rather than upfront — the agent only reads a
    small fraction of any non-trivial repo. The default refspec
    (``+refs/heads/*:refs/remotes/origin/*``) is used so every branch
    is trackable on fetch, and full commit history is kept so
    ``origin/main...HEAD`` merge-base lookups succeed on iteration runs
    where the task branch was created off an older main commit.
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
    """Bring a reused workspace back to a clean ``branch`` checkout.

    Refreshes ``origin``'s URL (the access token is freshly minted each
    invocation), fetches the latest base branch, hard-resets HEAD onto
    it, and removes any untracked files. Idempotent.
    """
    run_git("remote", "set-url", "origin", url, cwd=target)
    run_git("fetch", "origin", branch, cwd=target)
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


def impl_branch_name(spec_slug: str, run_id: str) -> str:
    """Conventional branch name for the unified implementation PR.

    One impl branch per run; every task in the run merges into it via
    GitHub's server-side merge API. Mirrors the existing
    ``aidlc/spec/{spec_slug}`` naming for spec PRs.
    """
    from common.ids import short_run_id  # noqa: PLC0415 - cheap import-time pin

    return f"aidlc/impl/{spec_slug}/{short_run_id(run_id)}"


def task_branch_name(task_id: str, spec_slug: str, run_id: str) -> str:
    """Conventional branch name for a single task within an impl run.

    Lives under ``aidlc/task/...`` rather than nested inside the impl
    branch's ref path: git stores refs as filesystem paths, so a ref
    named ``aidlc/impl/{slug}/{short_run_id}`` can't coexist with a
    second ref under ``aidlc/impl/{slug}/{short_run_id}/{task_id}``
    (the first would have to be both a file and a directory).

    Run-scoped via ``short_run_id`` so retries against the same
    ``(spec_slug, task_id)`` don't collide.
    """
    from common.ids import short_run_id  # noqa: PLC0415 - cheap import-time pin

    return f"aidlc/task/{spec_slug}/{short_run_id(run_id)}/{task_id.lower()}"


def create_branch(branch: str) -> None:
    """Create + check out a fresh branch off the current HEAD.

    Uses ``-B`` (capital) so a leftover branch of the same name from a
    prior session is force-recreated rather than failing with exit 128.
    """
    run_git("checkout", "-B", branch)


def checkout_impl_branch(branch: str) -> None:
    """Fetch + check out the run's impl branch as the task branch's base.

    Called after ``clone_repo`` (which lands on ``main``) and before
    ``create_branch`` for the task. The impl branch is created by the
    event projector on ``SPEC.APPROVED`` and starts identical to
    ``main`` — sibling-task merges advance it.
    """
    run_git("fetch", "origin", branch)
    run_git("checkout", "-B", branch, f"origin/{branch}")


def fetch_branch(branch: str) -> None:
    """Fetch ``branch`` from ``origin`` without changing HEAD."""
    run_git("fetch", "origin", branch)


def merge_remote_into_head(branch: str) -> bool:
    """Merge ``origin/{branch}`` into the current HEAD.

    Returns ``True`` when the merge completed cleanly (auto-merge with
    no conflict markers, or a fast-forward). Returns ``False`` when
    the merge left conflict markers in the working tree — caller is
    expected to either resolve them and commit, or run
    ``git merge --abort``.
    """
    run_git("fetch", "origin", branch)
    try:
        run_git("merge", f"origin/{branch}", "--no-edit")
    except RuntimeError:
        return not has_unmerged_paths()
    return True


def has_unmerged_paths() -> bool:
    """``True`` when ``git diff --name-only --diff-filter=U`` is non-empty."""
    return bool(unmerged_paths())


def unmerged_paths() -> list[str]:
    """List paths with unresolved merge conflicts in the working tree."""
    output = run_git("diff", "--name-only", "--diff-filter=U")
    return [line for line in output.splitlines() if line]


def abort_merge() -> None:
    """Abort an in-progress merge, restoring the working tree."""
    run_git("merge", "--abort")


def commit_changes(message: str) -> str:
    """Stage every change in the repo + create a commit. Return the SHA."""
    run_git("add", "-A")
    run_git("commit", "-m", message)
    return run_git("rev-parse", "HEAD").strip()


def push_branch(branch: str) -> None:
    """Push ``branch`` to ``origin``. Single-writer: a plain push suffices.

    Task branches are scoped to ``short_run_id`` and branched off the
    run's impl branch tip; no other container writes the same task
    branch, so the push is always fast-forward.
    """
    run_git("push", "--set-upstream", "origin", branch)


def has_uncommitted_changes() -> bool:
    """``True`` when the working tree has any uncommitted modifications."""
    return bool(run_git("status", "--porcelain").strip())


def agent_made_real_changes(spec_slug: str, *, base: str) -> bool:
    """``True`` when committed or uncommitted edits touch any path outside the spec tree.

    Compares ``HEAD`` against ``origin/{base}`` (typically the run's
    impl branch) so this catches edits the agent made and committed
    during its session, edits inherited from a prior run on the same
    branch, and edits still in the working tree. Spec docs under
    ``docs/specs/<spec_slug>/`` are excluded — they're merged via the
    spec PR before the implementer runs.
    """
    spec_prefix = f"docs/specs/{spec_slug}/"
    # ``rstrip`` instead of ``strip``: porcelain leads with two status chars
    # plus a space, so a leading space is meaningful and must survive trim.
    porcelain = run_git("status", "--porcelain").rstrip().splitlines()
    # ``XY <path>`` (or ``XY <old> -> <new>`` for renames); strip the flags.
    uncommitted = [line[3:].split(" -> ")[-1] for line in porcelain]
    committed = changed_paths(base=base)
    return any(not p.startswith(spec_prefix) for p in committed + uncommitted)


def changed_paths(*, base: str) -> list[str]:
    """Return paths changed on HEAD vs ``base`` (e.g. ``main``).

    Uses the ``base...HEAD`` triple-dot diff so deletions, additions, and
    modifications are all reported. The branch must be checked out and
    ``base`` must already be fetched (``reset_existing_clone`` and
    ``clone_repo`` both fetch ``origin/<base>`` for us).
    """
    output = run_git("diff", "--name-only", f"origin/{base}...HEAD")
    return [line for line in output.splitlines() if line]


def short_diff_summary() -> str:
    """Return ``git diff --stat`` for the most recent commit."""
    return run_git("show", "--stat", "--format=", "HEAD")


def shell_safe_join(args: list[str]) -> str:
    """Re-join ``args`` for log lines without breaking shell quoting."""
    return " ".join(shlex.quote(a) for a in args)


# ----------------------------------------------------------------------
# Iteration-mode helpers (iteration_count > 0).
#
# On iteration runs the implementer doesn't clone main + create a new
# branch — the task branch already exists on origin from the prior
# implementer run. Instead we fetch the existing branch and check it
# out, let Claude make fix commits, then post inline replies via
# repo_helper.
# ----------------------------------------------------------------------

PR_NUMBER_PATTERN = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)$")


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client (for invoking ``repo_helper``)."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str | None:
    """Lambda function name for ``repo_helper`` — empty when not wired in this env."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME") or None


def parse_pr_number(pr_url: str) -> int:
    """Extract the PR number from a github.com pull URL.

    Raises ``ValueError`` if the URL doesn't match the expected shape —
    the iteration_reactor only emits validated URLs so this is a
    defensive check, not user input.
    """
    match = PR_NUMBER_PATTERN.match(pr_url)
    if match is None:
        msg = f"unparseable pr_url: {pr_url!r}"
        raise ValueError(msg)
    return int(match.group(1))


def checkout_task_branch(branch: str) -> None:
    """Fetch + check out an existing task branch (iteration mode).

    Used when ``iteration_count > 0``. The branch exists on origin from
    the prior implementer run; we want HEAD to land on top of it (rather
    than recreating from main, which is what ``create_branch`` does for
    iteration_count == 0). Idempotent — ``-B`` recreates the local ref.
    """
    run_git("fetch", "origin", branch)
    run_git("checkout", "-B", branch, f"origin/{branch}")


def invoke_repo_helper(*, op: str, requestor_sub: str | None, **fields: Any) -> dict[str, Any]:
    """Synchronously invoke the ``repo_helper`` Lambda and return its result.

    Mirrors the Proposer's pattern (``proposer.app:lambda_client().invoke``).
    Raises ``RuntimeError`` on transport failure or when ``repo_helper``
    returns ``{"ok": false, ...}`` — iteration-mode callers want to fail
    loud rather than silently dropping a comment-reply or a check-runs
    fetch.
    """
    fn = repo_helper_function_name()
    if fn is None:
        msg = "AIDLC_REPO_HELPER_FUNCTION_NAME unset; cannot invoke repo_helper"
        raise RuntimeError(msg)
    payload: dict[str, Any] = {"input": {"op": op, "requestor_sub": requestor_sub, **fields}}
    response = lambda_client().invoke(
        FunctionName=fn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if not body.get("ok"):
        msg = f"repo_helper.{op} failed: {body.get('error')}"
        raise RuntimeError(msg)
    return body.get("result", {})


def post_inline_replies(
    *,
    repo: str,
    pr_number: int,
    requestor_sub: str | None,
    replies: list[tuple[int, str]],
) -> None:
    """Post a batch of PR-review-thread replies, one repo_helper call per reply.

    ``replies`` is a list of ``(comment_id, body)`` tuples. Failures on
    individual replies are logged but don't abort the batch — a single
    deleted upstream comment shouldn't drop the rest.
    """
    for comment_id, body in replies:
        try:
            invoke_repo_helper(
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
    *,
    repo: str,
    head_sha: str,
    requestor_sub: str | None,
) -> list[dict[str, Any]]:
    """Return only the failing check runs for ``head_sha`` — for prompt context."""
    result = invoke_repo_helper(
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
