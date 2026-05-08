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

from common.door import classify_paths
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
    """Push ``branch`` to ``origin``, recovering from non-fast-forward rejects.

    A re-run for the same ``(spec_slug, task_id)`` finds the remote branch
    populated from the prior run while the local branch was force-recreated
    off ``main``. The first push fails with ``! [rejected] ... non-fast-
    forward``. We fetch the remote ref and force-push with a lease — the
    lease (no explicit value) reads the just-fetched
    ``refs/remotes/origin/<branch>``, so a concurrent maintainer push
    between fetch and push fails loud rather than getting overwritten.
    """
    try:
        run_git("push", "--set-upstream", "origin", branch)
    except RuntimeError as exc:
        logger.info("push rejected; fetching and force-pushing", branch=branch, error=str(exc))
        run_git("fetch", "origin", branch)
        run_git("push", "--force-with-lease", "origin", branch)
        logger.info("force-pushed existing branch", branch=branch)


def has_uncommitted_changes() -> bool:
    """``True`` when the working tree has any uncommitted modifications."""
    return bool(run_git("status", "--porcelain").strip())


def agent_made_real_changes(spec_slug: str) -> bool:
    """``True`` when the agent's edits touch any path outside the spec tree.

    The platform writes the spec bundle to ``docs/specs/<spec_slug>/`` after
    the agent finishes, but those files come from the platform — not the
    agent. Re-runs that hit a Bedrock auth failure or otherwise short-
    circuit produce zero agent edits; we want to detect that and skip the
    PR-creation flow rather than emit a 1-line "diff" PR.
    """
    # ``rstrip`` instead of ``strip``: the porcelain format leads with status
    # flags ('` M`', '`A `', etc.), so a leading space is meaningful and
    # must survive the trim.
    porcelain = run_git("status", "--porcelain").rstrip()
    if not porcelain:
        return False
    spec_prefix = f"docs/specs/{spec_slug}/"
    for line in porcelain.splitlines():
        # ``git status --porcelain`` format: two status chars, a space, then
        # the path (and optionally `` -> path`` for renames).
        path = line[3:].split(" -> ")[-1]
        if not path.startswith(spec_prefix):
            return True
    return False


def changed_paths(*, base: str) -> list[str]:
    """Return paths changed on HEAD vs ``base`` (e.g. ``main``).

    Uses the ``base...HEAD`` triple-dot diff so deletions, additions, and
    modifications are all reported. The branch must be checked out and
    ``base`` must already be fetched (``reset_existing_clone`` and
    ``clone_repo`` both fetch ``origin/<base>`` for us).
    """
    output = run_git("diff", "--name-only", f"origin/{base}...HEAD")
    return [line for line in output.splitlines() if line]


def github_headers(token: str) -> dict[str, str]:
    """Standard GitHub REST headers for one bearer token."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def comment_on_pr(session: RepoSession, *, pr_number: int, body: str) -> None:
    """Post an issue-comment on a PR (PRs share the issues comment endpoint)."""
    url = f"{GITHUB_API}/repos/{session.target_repo}/issues/{pr_number}/comments"
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(url, headers=github_headers(session.access_token), json={"body": body})
        resp.raise_for_status()


def draft_explanation(categories: list[str]) -> str:
    """Body of the explanatory comment posted when draft mode is forced."""
    bullets = "\n".join(f"- `{c}`" for c in categories)
    return (
        "This PR is held in draft because the diff touches changes that the "
        "platform classifies as **one-way doors** (changes that are hard to "
        "reverse without significant cost):\n\n"
        f"{bullets}\n\n"
        'A maintainer should review the diff and mark the PR "Ready for '
        'review" before merging. See `packages/common/src/common/door.py` for '
        "the full taxonomy."
    )


def read_pr_html_url(session: RepoSession, pr_number: int) -> str:
    """Fetch a PR's ``html_url`` by number."""
    url = f"{GITHUB_API}/repos/{session.target_repo}/pulls/{pr_number}"
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.get(url, headers=github_headers(session.access_token))
        resp.raise_for_status()
        return str(resp.json()["html_url"])


def find_open_pr_for_branch(session: RepoSession, branch: str) -> int | None:
    """Return the open PR's number for ``branch``, or ``None`` if none exists.

    Used to make :func:`open_pr` idempotent across re-runs: when a prior
    invocation already opened a PR for this head, we reuse it rather than
    posting and getting a 422 "A pull request already exists" back.
    """
    owner = session.target_repo.split("/", 1)[0]
    url = f"{GITHUB_API}/repos/{session.target_repo}/pulls"
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.get(
            url,
            headers=github_headers(session.access_token),
            params={"head": f"{owner}:{branch}", "state": "open"},
        )
        resp.raise_for_status()
        body = resp.json()
    if not body:
        return None
    return int(body[0]["number"])


def open_pr(
    session: RepoSession,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
) -> str:
    """Open (or reuse) a PR via the GitHub REST API; return the PR HTML URL.

    When an open PR already exists for ``branch``, return its URL instead
    of creating a new one. The existing PR's title/body/draft state is
    left untouched — reviewers may have anchored comments on the original
    body, and we don't want to flip a "Ready for review" PR back to draft.

    For brand-new PRs, force draft mode when the diff touches any path
    matching the one-way door rules in :func:`common.door.classify_paths`
    — defense in depth on top of the Architect's stated ``door_class``.
    When the override engages, also post an explanatory first comment on
    the PR so the maintainer knows why it landed in draft.
    """
    existing_pr = find_open_pr_for_branch(session, branch)
    if existing_pr is not None:
        existing_url = read_pr_html_url(session, existing_pr)
        logger.info(
            "open_pr reused existing pr",
            target_repo=session.target_repo,
            branch=branch,
            pr_number=existing_pr,
        )
        return existing_url
    forced_categories = classify_paths(changed_paths(base=base))
    is_draft = draft or bool(forced_categories)
    override_engaged = bool(forced_categories) and not draft
    if override_engaged:
        logger.warning(
            "open_pr forcing draft mode",
            target_repo=session.target_repo,
            branch=branch,
            categories=forced_categories,
        )
    url = f"{GITHUB_API}/repos/{session.target_repo}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": branch,
        "base": base,
        "draft": is_draft,
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(url, headers=github_headers(session.access_token), json=payload)
        resp.raise_for_status()
        pr = resp.json()
    if override_engaged:
        comment_on_pr(
            session,
            pr_number=int(pr["number"]),
            body=draft_explanation(list(forced_categories)),
        )
    return str(pr["html_url"])


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

    The explicit ``branch:refs/remotes/origin/branch`` refspec is
    required because ``clone_repo`` shallow-clones with
    ``--branch main``, which configures
    ``remote.origin.fetch = +refs/heads/main:refs/remotes/origin/main``.
    A bare ``git fetch origin <task-branch>`` updates ``FETCH_HEAD``
    but doesn't populate ``refs/remotes/origin/<task-branch>``, so
    ``checkout -B <branch> origin/<branch>`` would fail with
    "is not a commit" without the explicit refspec.
    """
    run_git("fetch", "origin", f"{branch}:refs/remotes/origin/{branch}")
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
