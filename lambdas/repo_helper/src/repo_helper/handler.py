"""AgentCore Gateway target Lambda for git / GitHub operations.

This Lambda is registered with the per-agent gateway as a tool target and
dispatches on ``input.op``: ``open_pr``, ``comment_pr``, ``create_branch``,
``commit_files``, ``get_pr``. It receives a GitHub OAuth access token from
the AgentCore Gateway via the ``input.token`` field — the gateway inserts
the token from the OAuth2 credential provider before forwarding the call.

Phase 3 ships stub responses for every operation: the schema is real and
locked-in, the endpoints validate input, but the network calls happen in
Phase 6 once the implementer agent needs them. The Phase 3 success criterion
("manual MCP list_tools returns the catalog") is satisfied by the schema
alone.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = Logger(service="repo_helper")


class BaseOp(BaseModel):
    """Common configuration for every input model."""

    model_config = ConfigDict(extra="forbid", strict=True)


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
    """Lambda entrypoint. Validates input, returns a stub response."""
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
    logger.info("op validated (stub response)", extra={"op": op})
    return {
        "ok": True,
        "op": op,
        "result": {
            "stub": True,
            "echo": req.model_dump(),
            "note": "repo_helper is wired in Phase 3 but performs network calls in Phase 6.",
        },
    }


def error(kind: str, detail: object) -> dict[str, Any]:
    """Log a rejection and return the standard error envelope."""
    logger.warning("op rejected", extra={"kind": kind, "detail": detail})
    return {"ok": False, "error": {"kind": kind, "detail": detail}}
