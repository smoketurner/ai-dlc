"""AgentCore Gateway target Lambda for git / GitHub operations.

This Lambda is registered with each per-agent gateway as a tool target and
dispatches on ``input.op``: ``open_pr``, ``comment_pr``, ``create_branch``,
``commit_files``, ``get_pr``.

**Auth model**: a GitHub App is installed on each project repo. The Lambda
holds the App's credentials in Secrets Manager (ARN passed via
``AIDLC_GITHUB_APP_SECRET_ARN``) and mints installation-scoped access
tokens on demand via :mod:`common.github_app`. The token never appears in
input — agents (and the gateway) only know the target repo, not its
credentials.

Network calls hit api.github.com via ``httpx``. Errors are surfaced as the
standard ``{"ok": false, "error": ...}`` envelope so the calling agent can
decide whether to retry or raise.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import boto3
import httpx
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.github_app import (
    ACCEPT_HEADER,
    API_VERSION,
    GITHUB_API,
    HTTP_TIMEOUT,
    USER_AGENT,
    token_for_call,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="repo_helper")
tracer = Tracer(service="repo_helper")
metrics = Metrics(namespace="ai-dlc", service="repo_helper")


class BaseOp(BaseModel):
    """Common configuration for every input model.

    ``requestor_sub`` is the user's stable Cognito subject identifier
    threaded through from the dashboard / Step Functions input. When set,
    the Lambda asks AgentCore Identity (via ``GetWorkloadAccessTokenForUserId``
    + ``GetResourceOauth2Token``) to resolve it into the user's stored
    GitHub OAuth token. When ``None``, the Lambda falls back to the App's
    installation token (commits attributed to ``ai-dlc[bot]``).

    The sub is just a string identifier, not a credential — safe to
    persist in events / state-machine input. JWTs would be credentials
    and were intentionally avoided here.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    requestor_sub: str | None = Field(default=None, max_length=128)


class OpenPrInput(BaseOp):
    """Open a pull request on a GitHub repository."""

    op: Literal["open_pr"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    base: str = Field(min_length=1, max_length=128)
    head: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=65_536)


class CommentPrInput(BaseOp):
    """Add a comment to an existing pull request."""

    op: Literal["comment_pr"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=65_536)


class CreateBranchInput(BaseOp):
    """Create a branch off another branch via the GitHub API."""

    op: Literal["create_branch"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    branch: str = Field(min_length=1, max_length=128)
    base: str = Field(min_length=1, max_length=128)


class CommitFile(BaseModel):
    """One file to upsert in a single commit."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=2_000_000)


class CommitFilesInput(BaseOp):
    """Commit a set of file upserts to a branch in a single commit."""

    op: Literal["commit_files"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    branch: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)
    files: list[CommitFile] = Field(min_length=1, max_length=64)


class GetPrInput(BaseOp):
    """Read a pull request's metadata + state."""

    op: Literal["get_pr"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)


class CommentIssueInput(BaseOp):
    """Add a comment to an issue."""

    op: Literal["comment_issue"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    issue_number: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=65_536)


class LabelIssueInput(BaseOp):
    """Apply labels to an issue (additive — existing labels are preserved)."""

    op: Literal["label_issue"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    issue_number: int = Field(ge=1)
    labels: list[str] = Field(min_length=1, max_length=16)


class GetIssueInput(BaseOp):
    """Read an issue's title, body, labels, and state."""

    op: Literal["get_issue"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    issue_number: int = Field(ge=1)


class GetFileInput(BaseOp):
    """Read a file from a repo at a specific ref via the Contents API.

    Used by the Retrospector to read ``MEMORY.md`` / ``AGENTS.md`` straight
    from ``main`` (the source of truth) instead of from the architect's S3
    mirror — which lags by one architect-sync cycle and would lead to
    duplicate-bullet PRs in rapid retrospective fires.
    """

    op: Literal["get_file"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    path: str = Field(min_length=1, max_length=1024)
    ref: str = Field(default="main", min_length=1, max_length=256)


class CreateIssueInput(BaseOp):
    """Open a new GitHub issue, optionally backlinked to a parent issue.

    When ``parent_issue_url`` is set, a ``> Spawned from <url>`` blockquote
    is prepended to ``body`` (with ``by @<requestor>`` appended when
    ``requestor`` is set). Keeps the spawned-issue provenance consistent
    regardless of which agent builds the body.
    """

    op: Literal["create_issue"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=65_536)
    labels: list[str] = Field(default_factory=list, max_length=16)
    parent_issue_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        pattern=r"^https://github\.com/.+$",
    )
    requestor: str | None = Field(default=None, min_length=1, max_length=64)


class ListIssuesInput(BaseOp):
    """List open issues (optionally filtered by labels) for the cron backstop."""

    op: Literal["list_issues"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    labels: list[str] | None = Field(default=None, max_length=16)
    state: Literal["open", "closed", "all"] = "open"
    per_page: int = Field(default=30, ge=1, le=100)


class ListIssueCommentsInput(BaseOp):
    """List comments on an issue in chronological order.

    Used by the proposer to read its own prior synthesis comment + any
    follow-up reply when interpreting a conversational request to spawn
    issues from prior research.
    """

    op: Literal["list_issue_comments"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    issue_number: int = Field(ge=1)
    since: str | None = Field(default=None, max_length=32)
    per_page: int = Field(default=100, ge=1, le=100)


class GetPrDiffInput(BaseOp):
    """Fetch a PR's per-file diff metadata via the GitHub Files API.

    Used by Reviewer/Tester to read the diff text without needing a
    sandbox. Backed by ``GET /repos/{repo}/pulls/{n}/files``, which
    returns up to 100 files per page; this op follows pagination up to
    ``GET_PR_DIFF_FILE_CAP`` files. Each file's ``patch`` is bounded by
    ``GET_PR_DIFF_PATCH_TAIL_BYTES``.
    """

    op: Literal["get_pr_diff"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)


class GetPrArchiveUrlInput(BaseOp):
    """Resolve a short-lived signed tarball URL for a PR head.

    Used by Tester/Reviewer to hand a Code Interpreter sandbox session a
    URL it can download (and extract via ``tarfile``) without needing
    ``git`` installed. The handler resolves the PR's current ``head_sha``
    and asks GitHub for ``/repos/{repo}/tarball/{sha}``; GitHub responds
    with a 302 to a short-lived ``codeload.github.com`` URL that already
    carries its own signed token in the query string, so the bearer
    token never leaves this Lambda.
    """

    op: Literal["get_pr_archive_url"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)


class ListPrCommentsInput(BaseOp):
    """List PR-conversation comments (issue-style) for a pull request."""

    op: Literal["list_pr_comments"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)
    # ISO-8601 timestamp; only comments updated at/after this are returned.
    since: str | None = Field(default=None, max_length=32)


class ListPrReviewCommentsInput(BaseOp):
    """List inline review-thread comments (line-anchored) on a pull request."""

    op: Literal["list_pr_review_comments"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)


class ReplyPrReviewCommentInput(BaseOp):
    """Post a threaded reply under an existing PR review comment."""

    op: Literal["reply_pr_review_comment"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)
    comment_id: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=65_536)


class OpenSpecPrInput(BaseOp):
    """Open the spec PR for a run.

    Compound op: reads the three spec Markdown docs from S3
    (``specs/{spec_slug}/{requirements,design,tasks}.md``), creates a
    branch off ``base``, commits the docs into the repo under
    ``docs/specs/{spec_slug}/``, and opens a PR. Returns the PR's URL +
    number so the state-router can persist them on the run STATE row
    (``pr_url``) for the webhook ``gsi_pr`` lookup.
    """

    op: Literal["open_spec_pr"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    spec_slug: str = Field(min_length=1, max_length=128)
    spec_s3_prefix: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=64)
    base: str = Field(default="main", min_length=1, max_length=128)
    # When the run was triggered by a GitHub issue, the URL goes into
    # the PR body so GitHub renders a backlink in both directions
    # (issue timeline shows the PR; PR shows the source issue). We
    # deliberately don't use closing keywords ("Fixes #N") — the issue
    # stays open until task PRs land, not when the spec is approved.
    source_issue_url: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        pattern=r"^https://github\.com/.+$",
    )


class ListCheckRunsInput(BaseOp):
    """List GitHub Checks API check runs for a commit / branch.

    ``ref`` is a SHA, branch name, or tag the checks attach to.
    ``filter_conclusions`` narrows results to specific outcomes (e.g.
    ``["failure", "timed_out"]``); ``None`` returns every check.
    """

    op: Literal["list_check_runs"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    ref: str = Field(min_length=1, max_length=256)
    filter_conclusions: (
        list[
            Literal[
                "success",
                "failure",
                "neutral",
                "cancelled",
                "skipped",
                "timed_out",
                "action_required",
                "stale",
            ]
        ]
        | None
    ) = Field(default=None, max_length=8)


DISPATCH: dict[str, type[BaseOp]] = {
    "open_pr": OpenPrInput,
    "open_spec_pr": OpenSpecPrInput,
    "comment_pr": CommentPrInput,
    "create_branch": CreateBranchInput,
    "commit_files": CommitFilesInput,
    "get_pr": GetPrInput,
    "get_file": GetFileInput,
    "comment_issue": CommentIssueInput,
    "label_issue": LabelIssueInput,
    "get_issue": GetIssueInput,
    "create_issue": CreateIssueInput,
    "list_issues": ListIssuesInput,
    "list_issue_comments": ListIssueCommentsInput,
    "get_pr_diff": GetPrDiffInput,
    "get_pr_archive_url": GetPrArchiveUrlInput,
    "list_pr_comments": ListPrCommentsInput,
    "list_pr_review_comments": ListPrReviewCommentsInput,
    "reply_pr_review_comment": ReplyPrReviewCommentInput,
    "list_check_runs": ListCheckRunsInput,
}


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client (used by ``open_spec_pr``)."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding spec bundles + run artifacts."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


SPEC_DOCS = ("requirements", "design", "tasks")


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Lambda entrypoint. Validates input, dispatches to the GitHub API."""
    payload = event.get("input") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return error("invalid_event", "expected event.input to be a JSON object")
    op = payload.get("op")
    if op not in DISPATCH:
        return error("unknown_op", f"op must be one of {sorted(DISPATCH)}, got {op!r}")
    try:
        req = DISPATCH[op].model_validate(payload)
    except ValidationError as exc:
        return error("validation_error", json.loads(exc.json()))

    logger.info("dispatch", extra={"op": op, "on_behalf_of": req.requestor_sub is not None})
    try:
        with github_client(repo=target_repo(req), requestor_sub=req.requestor_sub) as client:
            return run_op(req, client)
    except httpx.HTTPStatusError as exc:
        return error(
            "github_http_error",
            {
                "status_code": exc.response.status_code,
                "body": safe_body(exc.response),
            },
        )
    except httpx.HTTPError as exc:
        return error("github_network_error", str(exc))


def target_repo(req: BaseOp) -> str:
    """Extract the ``owner/name`` repo from any validated op input."""
    repo = getattr(req, "repo", None)
    if not isinstance(repo, str):
        msg = f"op {type(req).__name__} has no repo field"
        raise TypeError(msg)
    return repo


def run_op(req: BaseOp, client: httpx.Client) -> dict[str, Any]:
    """Dispatch to the right operation function based on input type."""
    handler_fn = OP_HANDLERS.get(type(req))
    if handler_fn is None:
        msg = f"unhandled op type: {type(req).__name__}"
        raise RuntimeError(msg)
    op_name, fn = handler_fn
    return wrap(op_name, fn(req, client))


def open_pr(req: OpenPrInput, client: httpx.Client) -> dict[str, Any]:
    """Open a pull request."""
    response = client.post(
        f"/repos/{req.repo}/pulls",
        json={"title": req.title, "body": req.body, "head": req.head, "base": req.base},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "pr_number": body["number"],
        "pr_url": body["html_url"],
        "state": body["state"],
    }


def open_spec_pr(req: OpenSpecPrInput, client: httpx.Client) -> dict[str, Any]:
    """Read the three spec docs from S3, branch, commit them, open the PR.

    When the spec docs match what's already on ``base`` (a re-run that
    produced an identical bundle to a previously-merged spec), the new
    tree's SHA equals the base tree's SHA. Short-circuit before
    creating an empty commit + 0-file-change PR — return ``no_change``
    so the state-router can advance straight to ``spec_approved``.
    """
    docs = read_spec_docs(req.spec_s3_prefix)
    branch = f"aidlc/spec/{req.spec_slug}"
    files = [
        CommitFile(
            path=f"docs/specs/{req.spec_slug}/{name}.md",
            content=docs[name],
        )
        for name in SPEC_DOCS
    ]
    base_commit_sha = create_or_reuse_branch(req.repo, branch, req.base, client)
    base_tree_sha = commit_tree_sha(req.repo, base_commit_sha, client)
    tree_entries = [build_blob_entry(req.repo, f, client) for f in files]
    new_tree_sha = create_tree(req.repo, base_tree_sha, tree_entries, client)
    if new_tree_sha == base_tree_sha:
        return {
            "no_change": True,
            "spec_slug": req.spec_slug,
            "branch": branch,
            "base_commit_sha": base_commit_sha,
        }
    new_commit_sha = create_commit(
        req.repo,
        f"spec: {req.spec_slug}",
        new_tree_sha,
        base_commit_sha,
        client,
    )
    update_ref(req.repo, branch, new_commit_sha, client)
    title = f"spec: {req.spec_slug}"
    body = render_spec_pr_body(req.spec_slug, req.run_id, req.source_issue_url)
    existing_pr = find_open_pr(req.repo, branch=branch, base=req.base, client=client)
    if existing_pr is not None:
        # Spec-PR iteration: the architect re-ran on the same branch.
        # The fast-forward above already added the new commit; the PR
        # is now showing the new content automatically. Skip POST /pulls
        # (would 422 with "A pull request already exists for ...").
        return {
            "pr_number": existing_pr["number"],
            "pr_url": existing_pr["html_url"],
            "state": existing_pr["state"],
            "branch": branch,
            "commit_sha": new_commit_sha,
            "iteration": True,
        }
    response = client.post(
        f"/repos/{req.repo}/pulls",
        json={"title": title, "body": body, "head": branch, "base": req.base},
    )
    response.raise_for_status()
    pr = response.json()
    return {
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
        "state": pr["state"],
        "branch": branch,
        "commit_sha": new_commit_sha,
    }


def find_open_pr(
    repo: str,
    *,
    branch: str,
    base: str,
    client: httpx.Client,
) -> dict[str, Any] | None:
    """Return the open PR for ``head=branch, base=base`` if one exists."""
    owner = repo.split("/", 1)[0]
    response = client.get(
        f"/repos/{repo}/pulls",
        params={"head": f"{owner}:{branch}", "base": base, "state": "open"},
    )
    response.raise_for_status()
    prs = response.json()
    if isinstance(prs, list) and prs:
        return prs[0]
    return None


def read_spec_docs(spec_s3_prefix: str) -> dict[str, str]:
    """Read the three Markdown docs from ``s3://{bucket}/{prefix}{name}.md``."""
    bucket = artifacts_bucket()
    prefix = spec_s3_prefix if spec_s3_prefix.endswith("/") else f"{spec_s3_prefix}/"
    docs: dict[str, str] = {}
    for name in SPEC_DOCS:
        obj = s3_client().get_object(Bucket=bucket, Key=f"{prefix}{name}.md")
        docs[name] = obj["Body"].read().decode("utf-8")
    return docs


def create_or_reuse_branch(
    repo: str,
    branch: str,
    base: str,
    client: httpx.Client,
) -> str:
    """Create ``branch`` off ``base`` (or reuse it if it already exists).

    Returns the branch's tip commit SHA — caller chains it into the
    tree/commit/ref dance to land the spec docs on top.
    """
    existing = client.get(f"/repos/{repo}/git/refs/heads/{branch}")
    if existing.status_code == httpx.codes.OK:
        return str(existing.json()["object"]["sha"])
    base_ref = client.get(f"/repos/{repo}/git/refs/heads/{base}")
    base_ref.raise_for_status()
    base_sha = str(base_ref.json()["object"]["sha"])
    created = client.post(
        f"/repos/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    created.raise_for_status()
    return base_sha


def render_spec_pr_body(
    spec_slug: str,
    run_id: str,
    source_issue_url: str | None = None,
) -> str:
    """PR body that links the spec docs, the source issue, and the run.

    No closing keyword on the source-issue line — the issue stays open
    until task PRs are merged. The plain URL gives GitHub a clickable
    link + a back-reference in the issue's timeline.
    """
    paragraphs = [f"ai-dlc spec for `{spec_slug}` (run `{run_id}`)."]
    if source_issue_url:
        paragraphs.append(f"Source issue: {source_issue_url}")
    paragraphs.append(
        f"This PR contains three docs under `docs/specs/{spec_slug}/`:\n\n"
        "- `requirements.md`\n"
        "- `design.md`\n"
        "- `tasks.md`",
    )
    paragraphs.append(
        "Merging approves the spec; the platform will then dispatch each task as a separate PR.",
    )
    return "\n\n".join(paragraphs) + "\n"


def comment_pr(req: CommentPrInput, client: httpx.Client) -> dict[str, Any]:
    """Add a comment to an existing pull request."""
    response = client.post(
        f"/repos/{req.repo}/issues/{req.pr_number}/comments",
        json={"body": req.body},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "comment_id": body["id"],
        "comment_url": body["html_url"],
    }


def create_branch(req: CreateBranchInput, client: httpx.Client) -> dict[str, Any]:
    """Create a branch off another branch via the GitHub Git refs API."""
    base_ref = client.get(f"/repos/{req.repo}/git/refs/heads/{req.base}")
    base_ref.raise_for_status()
    base_sha = base_ref.json()["object"]["sha"]
    response = client.post(
        f"/repos/{req.repo}/git/refs",
        json={"ref": f"refs/heads/{req.branch}", "sha": base_sha},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "branch": req.branch,
        "ref": body["ref"],
        "sha": body["object"]["sha"],
    }


def commit_files(req: CommitFilesInput, client: httpx.Client) -> dict[str, Any]:
    """Commit a set of files to a branch in a single commit via the Git Data API.

    Uses the standard tree/commit/ref dance:
        1. GET branch ref → latest commit SHA + tree SHA.
        2. POST blobs for each file (one network call per file).
        3. POST tree built from the new blobs (parent = base tree).
        4. POST commit with the new tree (parent = base commit).
        5. PATCH branch ref to point at the new commit.
    """
    base_commit_sha = head_commit_sha(req.repo, req.branch, client)
    base_tree_sha = commit_tree_sha(req.repo, base_commit_sha, client)
    tree_entries = [build_blob_entry(req.repo, f, client) for f in req.files]
    new_tree_sha = create_tree(req.repo, base_tree_sha, tree_entries, client)
    new_commit_sha = create_commit(req.repo, req.message, new_tree_sha, base_commit_sha, client)
    update_ref(req.repo, req.branch, new_commit_sha, client)
    return {
        "branch": req.branch,
        "commit_sha": new_commit_sha,
        "files_written": len(req.files),
    }


def get_pr(req: GetPrInput, client: httpx.Client) -> dict[str, Any]:
    """Read a pull request's metadata + state."""
    response = client.get(f"/repos/{req.repo}/pulls/{req.pr_number}")
    response.raise_for_status()
    body = response.json()
    return {
        "pr_number": body["number"],
        "pr_url": body["html_url"],
        "state": body["state"],
        "merged": body["merged"],
        "title": body["title"],
        "head_sha": body["head"]["sha"],
    }


def get_file(req: GetFileInput, client: httpx.Client) -> dict[str, Any]:
    """Read ``path`` from ``repo`` at ``ref`` via the GitHub Contents API.

    Returns ``{content, sha, ref, exists}``:

    * ``exists=False`` + empty content/sha when the path doesn't exist at
      that ref. Lets callers distinguish "file missing" from "fetch
      failed" without parsing HTTP errors.
    * The Contents API returns base64-encoded content for files; this
      handler decodes it as UTF-8 (errors='replace') so the caller gets
      plain text. SHA is the blob SHA — useful for conditional writes.
    """
    response = client.get(
        f"/repos/{req.repo}/contents/{req.path}",
        params={"ref": req.ref},
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        return {"exists": False, "content": "", "sha": "", "ref": req.ref}
    response.raise_for_status()
    body = response.json()
    encoded = body.get("content", "")
    encoding = body.get("encoding", "base64")
    if encoding == "base64":
        decoded = base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
    else:
        decoded = str(encoded)
    return {
        "exists": True,
        "content": decoded,
        "sha": body.get("sha", ""),
        "ref": req.ref,
    }


def comment_issue(req: CommentIssueInput, client: httpx.Client) -> dict[str, Any]:
    """Add a comment to an issue."""
    response = client.post(
        f"/repos/{req.repo}/issues/{req.issue_number}/comments",
        json={"body": req.body},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "comment_id": body["id"],
        "comment_url": body["html_url"],
    }


def label_issue(req: LabelIssueInput, client: httpx.Client) -> dict[str, Any]:
    """Add labels to an issue (existing labels are preserved)."""
    response = client.post(
        f"/repos/{req.repo}/issues/{req.issue_number}/labels",
        json={"labels": req.labels},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "issue_number": req.issue_number,
        "labels": [label["name"] for label in body],
    }


def get_issue(req: GetIssueInput, client: httpx.Client) -> dict[str, Any]:
    """Read an issue's title, body, labels, and state."""
    response = client.get(f"/repos/{req.repo}/issues/{req.issue_number}")
    response.raise_for_status()
    body = response.json()
    return {
        "issue_number": body["number"],
        "issue_url": body["html_url"],
        "title": body["title"],
        "body": body.get("body") or "",
        "state": body["state"],
        "labels": [label["name"] for label in body.get("labels", [])],
        "user": body["user"]["login"] if body.get("user") else "",
    }


def create_issue(req: CreateIssueInput, client: httpx.Client) -> dict[str, Any]:
    """Open a new issue. Prepends a backlink blockquote when a parent is set."""
    body = render_create_issue_body(req.body, req.parent_issue_url, req.requestor)
    payload: dict[str, Any] = {"title": req.title, "body": body}
    if req.labels:
        payload["labels"] = req.labels
    response = client.post(f"/repos/{req.repo}/issues", json=payload)
    response.raise_for_status()
    issue = response.json()
    return {
        "issue_number": issue["number"],
        "issue_url": issue["html_url"],
        "state": issue["state"],
        "labels": [label["name"] for label in issue.get("labels", [])],
    }


def render_create_issue_body(
    body: str,
    parent_issue_url: str | None,
    requestor: str | None,
) -> str:
    """Prepend ``> Spawned from <url> by @<requestor>`` when a parent is set."""
    if not parent_issue_url:
        return body
    attribution = f" by @{requestor}" if requestor else ""
    backlink = f"> Spawned from {parent_issue_url}{attribution}\n\n"
    return backlink + body


def list_issues(req: ListIssuesInput, client: httpx.Client) -> dict[str, Any]:
    """List issues on a repo, optionally filtered by labels.

    GitHub's ``/issues`` endpoint mixes PRs and issues; we filter PRs out
    so callers (like Triage) only see real issues.
    """
    params: dict[str, str] = {"state": req.state, "per_page": str(req.per_page)}
    if req.labels:
        params["labels"] = ",".join(req.labels)
    response = client.get(f"/repos/{req.repo}/issues", params=params)
    response.raise_for_status()
    items = [item for item in response.json() if "pull_request" not in item]
    return {
        "issues": [
            {
                "issue_number": item["number"],
                "issue_url": item["html_url"],
                "title": item["title"],
                "labels": [label["name"] for label in item.get("labels", [])],
            }
            for item in items
        ],
    }


def list_issue_comments(
    req: ListIssueCommentsInput,
    client: httpx.Client,
) -> dict[str, Any]:
    """List comments on an issue in chronological order."""
    params: dict[str, str] = {"per_page": str(req.per_page)}
    if req.since:
        params["since"] = req.since
    response = client.get(
        f"/repos/{req.repo}/issues/{req.issue_number}/comments",
        params=params,
    )
    response.raise_for_status()
    return {
        "comments": [
            {
                "id": item["id"],
                "user": (item.get("user") or {}).get("login", ""),
                "user_type": (item.get("user") or {}).get("type", ""),
                "body": item.get("body") or "",
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "html_url": item.get("html_url"),
            }
            for item in response.json()
        ],
    }


def list_pr_comments(req: ListPrCommentsInput, client: httpx.Client) -> dict[str, Any]:
    """List PR-conversation comments (issue-style)."""
    params: dict[str, str] = {"per_page": "100"}
    if req.since:
        params["since"] = req.since
    response = client.get(f"/repos/{req.repo}/issues/{req.pr_number}/comments", params=params)
    response.raise_for_status()
    return {
        "comments": [
            {
                "id": item["id"],
                "user": (item.get("user") or {}).get("login", ""),
                "user_type": (item.get("user") or {}).get("type", ""),
                "body": item.get("body") or "",
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "html_url": item.get("html_url"),
            }
            for item in response.json()
        ],
    }


def list_pr_review_comments(
    req: ListPrReviewCommentsInput,
    client: httpx.Client,
) -> dict[str, Any]:
    """List inline review-thread comments on a PR (line-anchored)."""
    response = client.get(
        f"/repos/{req.repo}/pulls/{req.pr_number}/comments",
        params={"per_page": "100"},
    )
    response.raise_for_status()
    return {
        "comments": [
            {
                "id": item["id"],
                "user": (item.get("user") or {}).get("login", ""),
                "user_type": (item.get("user") or {}).get("type", ""),
                "body": item.get("body") or "",
                "path": item.get("path"),
                "line": item.get("line"),
                "original_line": item.get("original_line"),
                "commit_id": item.get("commit_id"),
                "in_reply_to_id": item.get("in_reply_to_id"),
                "pull_request_review_id": item.get("pull_request_review_id"),
                "created_at": item.get("created_at"),
                "html_url": item.get("html_url"),
            }
            for item in response.json()
        ],
    }


def reply_pr_review_comment(
    req: ReplyPrReviewCommentInput,
    client: httpx.Client,
) -> dict[str, Any]:
    """Reply in-thread to an existing PR review comment."""
    response = client.post(
        f"/repos/{req.repo}/pulls/{req.pr_number}/comments/{req.comment_id}/replies",
        json={"body": req.body},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "comment_id": body["id"],
        "comment_url": body["html_url"],
        "in_reply_to_id": body.get("in_reply_to_id"),
    }


def list_check_runs(req: ListCheckRunsInput, client: httpx.Client) -> dict[str, Any]:
    """List GitHub Checks API check runs for a commit / branch / tag."""
    response = client.get(
        f"/repos/{req.repo}/commits/{req.ref}/check-runs",
        params={"per_page": "100"},
    )
    response.raise_for_status()
    runs = response.json().get("check_runs", [])
    if req.filter_conclusions is not None:
        keep = set(req.filter_conclusions)
        runs = [r for r in runs if r.get("conclusion") in keep]
    return {
        "check_runs": [
            {
                "id": run["id"],
                "name": run.get("name", ""),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url"),
                "details_url": run.get("details_url"),
                "started_at": run.get("started_at"),
                "completed_at": run.get("completed_at"),
                "output": {
                    "title": (run.get("output") or {}).get("title"),
                    "summary": ((run.get("output") or {}).get("summary") or "")[:4096],
                },
            }
            for run in runs
        ],
    }


GET_PR_DIFF_FILE_CAP = 300
GET_PR_DIFF_PATCH_TAIL_BYTES = 4096
GET_PR_DIFF_PER_PAGE = 100


def get_pr_diff(req: GetPrDiffInput, client: httpx.Client) -> dict[str, Any]:
    """Return per-file diff metadata for a pull request.

    Pages through ``/repos/{repo}/pulls/{n}/files`` up to
    :data:`GET_PR_DIFF_FILE_CAP` files. Each file's ``patch`` is
    truncated to the last :data:`GET_PR_DIFF_PATCH_TAIL_BYTES` bytes
    (the tail is what reviewers anchor comments against). Files
    returned by the API without a ``patch`` (binary, or too large for
    GitHub to inline) carry ``patch=None`` and ``truncated=True``.
    """
    pr = client.get(f"/repos/{req.repo}/pulls/{req.pr_number}")
    pr.raise_for_status()
    head_sha = str(pr.json()["head"]["sha"])
    files: list[dict[str, Any]] = []
    page = 1
    while len(files) < GET_PR_DIFF_FILE_CAP:
        response = client.get(
            f"/repos/{req.repo}/pulls/{req.pr_number}/files",
            params={"per_page": str(GET_PR_DIFF_PER_PAGE), "page": str(page)},
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list) or not batch:
            break
        for entry in batch:
            files.append(diff_file_entry(entry))
            if len(files) >= GET_PR_DIFF_FILE_CAP:
                break
        if len(batch) < GET_PR_DIFF_PER_PAGE:
            break
        page += 1
    return {
        "head_sha": head_sha,
        "files": files,
        "files_truncated": len(files) >= GET_PR_DIFF_FILE_CAP,
    }


def diff_file_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Project a GitHub Files-API entry into the agent-facing shape."""
    patch = entry.get("patch")
    truncated = patch is None
    if isinstance(patch, str) and len(patch.encode("utf-8")) > GET_PR_DIFF_PATCH_TAIL_BYTES:
        patch = patch.encode("utf-8")[-GET_PR_DIFF_PATCH_TAIL_BYTES:].decode(
            "utf-8", errors="replace"
        )
        truncated = True
    return {
        "filename": entry.get("filename", ""),
        "status": entry.get("status", ""),
        "additions": int(entry.get("additions", 0)),
        "deletions": int(entry.get("deletions", 0)),
        "patch": patch,
        "truncated": truncated,
        "previous_filename": entry.get("previous_filename"),
    }


def get_pr_archive_url(req: GetPrArchiveUrlInput, client: httpx.Client) -> dict[str, Any]:
    """Return a short-lived signed tarball URL pinned to the PR's head SHA.

    GitHub's ``/repos/{repo}/tarball/{ref}`` endpoint responds with a
    302 to ``codeload.github.com``; the redirect URL embeds its own
    short-lived token, so the caller (typically a sandbox session) can
    download without an ``Authorization`` header. The Lambda's bearer
    token never leaves this function.
    """
    pr = client.get(f"/repos/{req.repo}/pulls/{req.pr_number}")
    pr.raise_for_status()
    head_sha = str(pr.json()["head"]["sha"])
    response = client.get(f"/repos/{req.repo}/tarball/{head_sha}")
    if response.status_code not in (httpx.codes.FOUND, httpx.codes.SEE_OTHER):
        response.raise_for_status()
    archive_url = response.headers.get("location")
    if not archive_url:
        msg = "github tarball response missing Location header"
        raise RuntimeError(msg)
    return {"head_sha": head_sha, "archive_url": archive_url}


def head_commit_sha(repo: str, branch: str, client: httpx.Client) -> str:
    """Return the SHA of the latest commit on ``branch``."""
    response = client.get(f"/repos/{repo}/git/refs/heads/{branch}")
    response.raise_for_status()
    return response.json()["object"]["sha"]


def commit_tree_sha(repo: str, commit_sha: str, client: httpx.Client) -> str:
    """Return the tree SHA referenced by ``commit_sha``."""
    response = client.get(f"/repos/{repo}/git/commits/{commit_sha}")
    response.raise_for_status()
    return response.json()["tree"]["sha"]


def build_blob_entry(repo: str, file: CommitFile, client: httpx.Client) -> dict[str, str]:
    """Create a blob and return the tree entry that points at it."""
    blob = client.post(
        f"/repos/{repo}/git/blobs",
        json={
            "content": base64.b64encode(file.content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        },
    )
    blob.raise_for_status()
    return {"path": file.path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}


def create_tree(
    repo: str,
    base_tree_sha: str,
    entries: list[dict[str, str]],
    client: httpx.Client,
) -> str:
    """Create a new tree on top of ``base_tree_sha`` and return its SHA."""
    response = client.post(
        f"/repos/{repo}/git/trees",
        json={"base_tree": base_tree_sha, "tree": entries},
    )
    response.raise_for_status()
    return response.json()["sha"]


def create_commit(
    repo: str,
    message: str,
    tree_sha: str,
    parent_sha: str,
    client: httpx.Client,
) -> str:
    """Create a commit with ``tree_sha`` as content and ``parent_sha`` as parent."""
    response = client.post(
        f"/repos/{repo}/git/commits",
        json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
    )
    response.raise_for_status()
    return response.json()["sha"]


def update_ref(repo: str, branch: str, commit_sha: str, client: httpx.Client) -> None:
    """Fast-forward ``branch`` to point at ``commit_sha``."""
    response = client.patch(
        f"/repos/{repo}/git/refs/heads/{branch}",
        json={"sha": commit_sha, "force": False},
    )
    response.raise_for_status()


def github_client(*, repo: str, requestor_sub: str | None) -> httpx.Client:
    """Build an httpx client authenticated for ``repo``.

    Picks a user-on-behalf-of token via AgentCore Identity if
    ``requestor_sub`` resolves to a linked user; otherwise falls back to
    the App's installation token. See :func:`common.github_app.token_for_call`.
    """
    token = token_for_call(repo=repo, requestor_sub=requestor_sub)
    return httpx.Client(
        base_url=GITHUB_API,
        timeout=HTTP_TIMEOUT,
        headers={
            "Accept": ACCEPT_HEADER,
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        },
    )


def safe_body(response: httpx.Response) -> str | dict[str, Any]:
    """Best-effort body extraction that never raises.

    ``json.JSONDecodeError`` is a subclass of ``ValueError``, so the
    second clause is redundant — the parenthesised form is here only
    to make intent explicit and to keep the bare-except lint quiet.
    """
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return response.text[:1024]


def wrap(op: str, result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful op result in the standard envelope."""
    return {"ok": True, "op": op, "result": result}


def error(kind: str, detail: object) -> dict[str, Any]:
    """Log a rejection and return the standard error envelope."""
    logger.warning("op rejected", extra={"kind": kind, "detail": detail})
    return {"ok": False, "error": {"kind": kind, "detail": detail}}


# Type-keyed dispatch table — defined at module bottom so all op functions
# above it are bound. Keeps ``run_op`` complexity in check as ops grow.
OpHandler = Callable[[Any, httpx.Client], dict[str, Any]]
OP_HANDLERS: dict[type[BaseOp], tuple[str, OpHandler]] = {
    OpenPrInput: ("open_pr", open_pr),
    OpenSpecPrInput: ("open_spec_pr", open_spec_pr),
    CommentPrInput: ("comment_pr", comment_pr),
    CreateBranchInput: ("create_branch", create_branch),
    CommitFilesInput: ("commit_files", commit_files),
    GetPrInput: ("get_pr", get_pr),
    GetFileInput: ("get_file", get_file),
    CommentIssueInput: ("comment_issue", comment_issue),
    LabelIssueInput: ("label_issue", label_issue),
    GetIssueInput: ("get_issue", get_issue),
    CreateIssueInput: ("create_issue", create_issue),
    ListIssuesInput: ("list_issues", list_issues),
    ListIssueCommentsInput: ("list_issue_comments", list_issue_comments),
    GetPrDiffInput: ("get_pr_diff", get_pr_diff),
    GetPrArchiveUrlInput: ("get_pr_archive_url", get_pr_archive_url),
    ListPrCommentsInput: ("list_pr_comments", list_pr_comments),
    ListPrReviewCommentsInput: ("list_pr_review_comments", list_pr_review_comments),
    ReplyPrReviewCommentInput: ("reply_pr_review_comment", reply_pr_review_comment),
    ListCheckRunsInput: ("list_check_runs", list_check_runs),
}
