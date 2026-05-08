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

from common.event_emit import publish
from common.events import EventEnvelope, RunCompleted
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ProposerInput
from proposer.agent import propose, propose_research
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

    Schedule / regression: out-of-pipeline; no SDLC state machine to
    advance. Research: in-pipeline; we emit ``RUN.COMPLETED`` so the
    projector advances the run state ``proposer_running`` → ``done``.
    Exceptions are logged either way and the async task is still
    acknowledged so the microVM releases its session.
    """
    try:
        if payload.trigger_reason == "research":
            run_research(payload)
        else:
            run_scheduled(payload)
    except Exception:
        logger.exception("proposer run failed", run_id=payload.run_id)
    finally:
        app.complete_async_task(async_task_id)


def run_scheduled(payload: ProposerInput) -> None:
    """Schedule / regression path — propose from telemetry, open a PR if any edits."""
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


def run_research(payload: ProposerInput) -> None:
    """Research path — synthesise the issue's URLs, comment on the issue, optional PR."""
    if payload.intent is None or payload.issue_number is None:
        msg = "research trigger requires intent + issue_number"
        raise ValueError(msg)
    proposal = propose_research(
        project_slug=payload.project_slug,
        intent=payload.intent,
        issue_number=payload.issue_number,
        run_id=payload.run_id,
        target_repo=payload.target_repo,
        triggering_comment_body=payload.triggering_comment_body,
        triggering_commenter=payload.triggering_commenter,
    )
    if proposal.summary_comment.strip():
        post_research_comment(payload=payload, body=proposal.summary_comment)
    else:
        logger.warning("research proposal had empty summary_comment", run_id=payload.run_id)
    spawned_issue_urls = create_proposed_issues(payload=payload, proposal=proposal)
    pr_url: str | None = None
    if proposal.edits:
        pr_url = open_proposal_pr(payload=payload, proposal=proposal)
    publish_run_completed(payload, pr_url=pr_url, spawned_issue_urls=spawned_issue_urls)


def post_research_comment(*, payload: ProposerInput, body: str) -> None:
    """Post the synthesis as a comment on the source issue via repo_helper."""
    invoke_repo_helper(
        op="comment_issue",
        repo=payload.target_repo,
        issue_number=payload.issue_number,
        body=body,
    )


def create_proposed_issues(*, payload: ProposerInput, proposal: Proposal) -> list[str]:
    """Spawn one issue per ``proposal.proposed_issues`` entry. Returns URLs.

    Each spawned issue is backlinked to the parent via
    ``parent_issue_url`` so ``repo_helper.create_issue`` injects the
    ``> Spawned from <url> by @<requestor>`` blockquote consistently.
    """
    if not proposal.proposed_issues:
        return []
    parent_url = parent_issue_url(payload)
    requestor = payload.triggering_commenter or None
    spawned: list[str] = []
    for proposed in proposal.proposed_issues:
        out = invoke_repo_helper(
            op="create_issue",
            repo=payload.target_repo,
            title=proposed.title,
            body=proposed.body,
            labels=list(proposed.labels) or ["aidlc-spawned"],
            parent_issue_url=parent_url,
            requestor=requestor,
        )
        issue_url = out.get("result", {}).get("issue_url")
        if isinstance(issue_url, str):
            spawned.append(issue_url)
    logger.info(
        "spawned issues from proposal",
        run_id=payload.run_id,
        issue_count=len(spawned),
    )
    return spawned


def parent_issue_url(payload: ProposerInput) -> str:
    """Derive the parent issue's URL from ``target_repo`` + ``issue_number``."""
    return f"https://github.com/{payload.target_repo}/issues/{payload.issue_number}"


def publish_run_completed(
    payload: ProposerInput,
    *,
    pr_url: str | None,
    spawned_issue_urls: list[str] | None = None,
) -> None:
    """Emit ``RUN.COMPLETED`` so the projector advances the run to ``done``.

    ``spawned_issue_urls`` is logged but not surfaced on the event —
    spawned issues are inert work items, the run completes regardless of
    how many were created.
    """
    spawned = spawned_issue_urls or []
    envelope = EventEnvelope[RunCompleted](
        event_id=new_event_id(),
        type="RUN.COMPLETED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="proposer",
        payload=RunCompleted(
            project_slug=payload.project_slug,
            spec_slug=f"research-issue-{payload.issue_number}",
            tasks_completed=1 if pr_url else 0,
        ),
    )
    publish(envelope)
    if spawned:
        logger.info(
            "research run completed with spawned issues",
            run_id=payload.run_id,
            spawned_count=len(spawned),
        )


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
