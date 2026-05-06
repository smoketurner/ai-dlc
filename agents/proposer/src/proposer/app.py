"""AgentCore Runtime entrypoint for the Proposer.

The Proposer runs out of the main SDLC pipeline — invoked by an EventBridge
schedule (weekly) and on alerts from the eval-regression alarm. The
entrypoint:

  1. Validates the input as :class:`ProposerInput`.
  2. Asks the Strands agent for a :class:`Proposal`.
  3. If the proposal contains edits, calls ``repo_helper`` (via Lambda
     invoke) to create a branch, commit the proposed file contents, and
     open a PR. Otherwise short-circuits with ``proposal_made=False``.
  4. Returns a :class:`ProposerResult` for downstream auditing.

The Proposer authenticates as ``ai-dlc[bot]`` (installation token) — its
PRs are explicitly bot-attributed because they're system-initiated and
the requestor concept doesn't apply (no human triggered the cycle).
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import ProposerInput, ProposerResult
from proposer.agent import propose
from proposer.proposal import FileEdit, Proposal

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

BRANCH_SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client (for invoking repo_helper)."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str:
    """Lambda function name of the repo_helper tool."""
    return os.environ["AIDLC_REPO_HELPER_FUNCTION_NAME"]


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Proposer entrypoint. Returns a JSON-serialisable ProposerResult."""
    payload = ProposerInput.model_validate(event)
    logger.info(
        "proposer invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        trigger_reason=payload.trigger_reason,
    )

    proposal = propose(
        project_slug=payload.project_slug,
        trigger_reason=payload.trigger_reason,
        lookback_days=payload.evals_lookback_days,
        run_id=payload.run_id,
    )

    if not proposal.edits:
        logger.info("proposer found no actionable signal", run_id=payload.run_id)
        return ProposerResult(
            proposal_made=False,
            target_files=[],
            summary=proposal.rationale[:2048],
            session_id=payload.run_id,
        ).model_dump()

    pr_url = open_proposal_pr(payload=payload, proposal=proposal)
    logger.info("proposal opened", run_id=payload.run_id, pr_url=pr_url)
    return ProposerResult(
        proposal_made=True,
        pr_url=pr_url,
        target_files=[edit.target_file for edit in proposal.edits],
        summary=proposal.rationale[:2048],
        session_id=payload.run_id,
    ).model_dump()


def open_proposal_pr(*, payload: ProposerInput, proposal: Proposal) -> str:
    """Create a branch, commit edits, open a PR — return the PR URL."""
    branch = branch_name(run_id=payload.run_id)
    invoke_repo_helper(
        op="create_branch",
        repo=payload.target_repo,
        branch=branch,
        base=payload.base_branch,
    )
    invoke_repo_helper(
        op="commit_files",
        repo=payload.target_repo,
        branch=branch,
        message=f"proposer: {proposal.pr_title}",
        files=[edit_to_dict(edit) for edit in proposal.edits],
    )
    out = invoke_repo_helper(
        op="open_pr",
        repo=payload.target_repo,
        base=payload.base_branch,
        head=branch,
        title=proposal.pr_title,
        body=proposal.pr_body,
    )
    pr_url = out.get("result", {}).get("pr_url")
    if not isinstance(pr_url, str):
        msg = f"open_pr did not return a pr_url: {out!r}"
        raise TypeError(msg)
    return pr_url


def branch_name(*, run_id: str) -> str:
    """Generate a deterministic branch name for the proposer's PR."""
    safe = BRANCH_SLUG_PATTERN.sub("-", run_id.lower())
    return f"proposer/{safe}"


def edit_to_dict(edit: FileEdit) -> dict[str, str]:
    """Convert a ``FileEdit`` to the shape ``repo_helper.commit_files`` expects."""
    return {"path": edit.target_file, "content": edit.proposed_content}


def invoke_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
    """Invoke the repo_helper Lambda with one op + raise on the standard envelope."""
    response = lambda_client().invoke(
        FunctionName=repo_helper_function_name(),
        InvocationType="RequestResponse",
        Payload=json.dumps({"input": {"op": op, **fields}}).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if not body.get("ok"):
        msg = f"repo_helper.{op} failed: {body!r}"
        raise RuntimeError(msg)
    return body


if __name__ == "__main__":
    app.run()
