"""AgentCore Runtime entrypoint for the Proposer.

The Proposer runs out of the main SDLC pipeline — invoked by an EventBridge
schedule (weekly) and on alerts from the eval-regression alarm. The
entrypoint:

  1. Validates the input as :class:`ProposerInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the proposal runs.
  3. Spawns a daemon thread that asks the Strands agent for a
     :class:`Proposal`, opens a PR via ``repo_helper`` if there are
     edits, logs the outcome, and acknowledges the async task.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms.

The Proposer authenticates as ``ai-dlc[bot]`` (installation token) — its
PRs are explicitly bot-attributed because they're system-initiated and
the requestor concept doesn't apply (no human triggered the cycle).
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import ProposerInput
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
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = ProposerInput.model_validate(event)
    logger.info(
        "proposer invoked",
        run_id=payload.run_id,
        project_slug=payload.project_slug,
        trigger_reason=payload.trigger_reason,
    )
    task_id = app.add_async_task("proposer_run", {"run_id": payload.run_id})
    threading.Thread(
        target=run_proposer,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_proposer(payload: ProposerInput, async_task_id: int) -> None:
    """Body of the proposer run — opens a PR if there are actionable edits.

    Out-of-pipeline: no SDLC state machine to advance; an exception is
    logged and the async task is still acknowledged so the microVM
    can release its session. The EventBridge / alarm caller has its
    own retry semantics if a periodic run needs to re-fire.
    """
    try:
        proposal = propose(
            project_slug=payload.project_slug,
            trigger_reason=payload.trigger_reason,
            lookback_days=payload.evals_lookback_days,
            run_id=payload.run_id,
        )
        if not proposal.edits:
            logger.info("proposer found no actionable signal", run_id=payload.run_id)
            return
        pr_url = open_proposal_pr(payload=payload, proposal=proposal)
        logger.info("proposal opened", run_id=payload.run_id, pr_url=pr_url)
    except Exception:
        logger.exception("proposer run failed", run_id=payload.run_id)
    finally:
        app.complete_async_task(async_task_id)


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
