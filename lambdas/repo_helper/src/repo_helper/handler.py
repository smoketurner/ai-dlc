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


class ListIssuesInput(BaseOp):
    """List open issues (optionally filtered by labels) for the cron backstop."""

    op: Literal["list_issues"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    labels: list[str] | None = Field(default=None, max_length=16)
    state: Literal["open", "closed", "all"] = "open"
    per_page: int = Field(default=30, ge=1, le=100)


class MintCloneTokenInput(BaseOp):
    """Mint a short-lived authenticated clone URL for a PR head.

    Used by Tester/Reviewer to hand a Code Interpreter sandbox session a
    URL it can ``git clone`` directly. The returned token is the same
    bearer ``token_for_call`` produces for any other op — installation
    token by default, user-OBO when ``requestor_sub`` resolves to a
    linked user. The token is embedded as the userinfo component of the
    URL (``https://x-access-token:<token>@github.com/<repo>.git``); the
    response field carries it in plaintext, so callers MUST avoid
    logging the result.
    """

    op: Literal["mint_clone_token"]
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
    (``spec_pr_url``) for the webhook to match against later.
    """

    op: Literal["open_spec_pr"]
    repo: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    spec_slug: str = Field(min_length=1, max_length=128)
    spec_s3_prefix: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=64)
    base: str = Field(default="main", min_length=1, max_length=128)


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
    "comment_issue": CommentIssueInput,
    "label_issue": LabelIssueInput,
    "get_issue": GetIssueInput,
    "list_issues": ListIssuesInput,
    "mint_clone_token": MintCloneTokenInput,
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
    """Read the three spec docs from S3, branch, commit them, open the PR."""
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
    new_commit_sha = create_commit(
        req.repo,
        f"spec: {req.spec_slug}",
        new_tree_sha,
        base_commit_sha,
        client,
    )
    update_ref(req.repo, branch, new_commit_sha, client)
    title = f"spec: {req.spec_slug}"
    body = render_spec_pr_body(req.spec_slug, req.run_id)
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


def render_spec_pr_body(spec_slug: str, run_id: str) -> str:
    """PR body that links the spec docs and the dashboard run page."""
    return (
        f"ai-dlc spec for `{spec_slug}` (run `{run_id}`).\n\n"
        f"This PR contains three docs under `docs/specs/{spec_slug}/`:\n\n"
        f"- `requirements.md`\n"
        f"- `design.md`\n"
        f"- `tasks.md`\n\n"
        f"Merging approves the spec; the platform will then dispatch each task as a separate PR.\n"
    )


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


def mint_clone_token(req: MintCloneTokenInput, client: httpx.Client) -> dict[str, Any]:
    """Resolve a PR's head SHA and return an authenticated clone URL.

    The token embedded in the URL is the same bearer that authenticated
    this Lambda call (cached by :func:`common.github_app.token_for_call`).
    The response carries the token in plaintext so the calling agent can
    hand it to a Code Interpreter sandbox; callers MUST treat the result
    as a credential and avoid logging it.
    """
    response = client.get(f"/repos/{req.repo}/pulls/{req.pr_number}")
    response.raise_for_status()
    head_sha = str(response.json()["head"]["sha"])
    token = token_for_call(repo=req.repo, requestor_sub=req.requestor_sub)
    return {
        "clone_url": f"https://x-access-token:{token}@github.com/{req.repo}.git",
        "head_sha": head_sha,
    }


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
    """Best-effort body extraction that never raises."""
    try:
        return response.json()
    except ValueError, json.JSONDecodeError:
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
    CommentIssueInput: ("comment_issue", comment_issue),
    LabelIssueInput: ("label_issue", label_issue),
    GetIssueInput: ("get_issue", get_issue),
    ListIssuesInput: ("list_issues", list_issues),
    MintCloneTokenInput: ("mint_clone_token", mint_clone_token),
    ListPrCommentsInput: ("list_pr_comments", list_pr_comments),
    ListPrReviewCommentsInput: ("list_pr_review_comments", list_pr_review_comments),
    ReplyPrReviewCommentInput: ("reply_pr_review_comment", reply_pr_review_comment),
    ListCheckRunsInput: ("list_check_runs", list_check_runs),
}
