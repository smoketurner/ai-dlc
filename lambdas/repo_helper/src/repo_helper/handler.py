"""AgentCore Gateway target Lambda for git / GitHub operations.

This Lambda is registered with each per-agent gateway as a tool target and
dispatches on ``input.op``: ``open_pr``, ``comment_pr``, ``create_branch``,
``commit_files``, ``get_pr``.

**Auth model**: a GitHub App is installed on each project repo. The Lambda
holds the App's credentials in Secrets Manager (ARN passed via
``AIDLC_GITHUB_APP_SECRET_ARN``) and mints installation-scoped access
tokens on demand via :mod:`repo_helper.auth`. The token never appears in
input — agents (and the gateway) only know the target repo, not its
credentials.

Network calls hit api.github.com via ``httpx``. Errors are surfaced as the
standard ``{"ok": false, "error": ...}`` envelope so the calling agent can
decide whether to retry or raise.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Literal

import httpx
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from repo_helper.auth import (
    ACCEPT_HEADER,
    API_VERSION,
    GITHUB_API,
    HTTP_TIMEOUT,
    USER_AGENT,
    token_for_call,
)

logger = Logger(service="repo_helper")


class BaseOp(BaseModel):
    """Common configuration for every input model.

    ``requestor_jwt`` is the user's identity token (Cognito ID token)
    threaded through from the dashboard / Step Functions input. When set,
    the Lambda asks AgentCore Identity to resolve it into the user's
    GitHub OAuth token (commits attributed to them). When ``None``, the
    Lambda falls back to the App's installation token (commits attributed
    to ``ai-dlc[bot]``).
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    requestor_jwt: SecretStr | None = Field(default=None)


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


DISPATCH: dict[str, type[BaseOp]] = {
    "open_pr": OpenPrInput,
    "comment_pr": CommentPrInput,
    "create_branch": CreateBranchInput,
    "commit_files": CommitFilesInput,
    "get_pr": GetPrInput,
}


@logger.inject_lambda_context(log_event=False)
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

    logger.info("dispatch", extra={"op": op, "on_behalf_of": req.requestor_jwt is not None})
    try:
        with github_client(repo=target_repo(req), requestor_jwt=req.requestor_jwt) as client:
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
    """Dispatch to the right operation function."""
    if isinstance(req, OpenPrInput):
        return wrap("open_pr", open_pr(req, client))
    if isinstance(req, CommentPrInput):
        return wrap("comment_pr", comment_pr(req, client))
    if isinstance(req, CreateBranchInput):
        return wrap("create_branch", create_branch(req, client))
    if isinstance(req, CommitFilesInput):
        return wrap("commit_files", commit_files(req, client))
    if isinstance(req, GetPrInput):
        return wrap("get_pr", get_pr(req, client))
    msg = f"unhandled op type: {type(req).__name__}"
    raise RuntimeError(msg)


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


def github_client(*, repo: str, requestor_jwt: SecretStr | None) -> httpx.Client:
    """Build an httpx client authenticated for ``repo``.

    Picks a user-on-behalf-of token via AgentCore Identity if
    ``requestor_jwt`` resolves to a linked user; otherwise falls back to
    the App's installation token. See :func:`repo_helper.auth.token_for_call`.
    """
    token = token_for_call(
        repo=repo,
        requestor_jwt=requestor_jwt.get_secret_value() if requestor_jwt is not None else None,
    )
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
