"""AgentCore Runtime entrypoint for the Proposer.

The Proposer is invoked when triage classifies an issue as
``research`` — the agent reads the URLs in the issue body, synthesises
findings into a comment on the issue, optionally opens a PR with
MEMORY.md / prompt edits, and may spawn follow-up issues when the
triggering comment asks for it. The entrypoint:

  1. Validates the input as :class:`ProposerInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the synthesis runs.
  3. Spawns a daemon thread under a copied :class:`contextvars.Context`
     that opens the per-agent gateway MCP session, runs the research
     flow, posts the synthesis comment via ``repo_helper``, opens a
     PR if there are edits, and emits ``RUN.COMPLETED`` so the
     projector advances the run state.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms.

``contextvars.copy_context()`` carries the runtime's
``WorkloadAccessToken`` ContextVar into the daemon thread so
:func:`common.gateway_tools.fetch_gateway_token` can exchange it for a
Cognito M2M JWT via AgentCore Identity. The Proposer authenticates as
``ai-dlc[bot]`` (installation token) downstream of the gateway.
"""

from __future__ import annotations

import contextvars
import re
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from common.event_emit import publish
from common.events import EventEnvelope, RunCompleted
from common.gateway_tools import call_gateway_tool, extract_envelope, gateway_mcp_client
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import ProposerInput
from proposer.agent import build_agent, propose_research
from proposer.proposal import FileEdit, Proposal

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

BRANCH_SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")


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
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_proposer, payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_proposer(payload: ProposerInput, async_task_id: int) -> None:
    """Body of the proposer run — research path only.

    Research is in-pipeline; we emit ``RUN.COMPLETED`` so the projector
    advances the run state ``proposer_running`` → ``done``. Exceptions
    are logged and the async task is still acknowledged so the microVM
    releases its session.
    """
    try:
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            run_research(payload, mcp_client=mcp_client)
    except Exception:
        logger.exception("proposer run failed", run_id=payload.run_id)
    finally:
        app.complete_async_task(async_task_id)


def run_research(payload: ProposerInput, *, mcp_client: MCPClient) -> None:
    """Research path — synthesise the issue's URLs, comment on the issue, optional PR."""
    if payload.intent is None or payload.issue_number is None:
        msg = "research trigger requires intent + issue_number"
        raise ValueError(msg)
    agent = build_agent(payload.run_id, mcp_client=mcp_client)
    proposal = propose_research(
        agent,
        project_slug=payload.project_slug,
        intent=payload.intent,
        issue_number=payload.issue_number,
        target_repo=payload.target_repo,
        triggering_comment_body=payload.triggering_comment_body,
        triggering_commenter=payload.triggering_commenter,
    )
    if proposal.summary_comment.strip():
        post_research_comment(mcp_client, payload=payload, body=proposal.summary_comment)
    else:
        logger.warning("research proposal had empty summary_comment", run_id=payload.run_id)
    spawned_issue_urls = create_proposed_issues(mcp_client, payload=payload, proposal=proposal)
    pr_url: str | None = None
    if proposal.edits:
        pr_url = open_proposal_pr(mcp_client, payload=payload, proposal=proposal)
    publish_run_completed(payload, pr_url=pr_url, spawned_issue_urls=spawned_issue_urls)


def post_research_comment(
    mcp_client: MCPClient,
    *,
    payload: ProposerInput,
    body: str,
) -> None:
    """Post the synthesis as a comment on the source issue via repo_helper."""
    invoke_repo_helper(
        mcp_client,
        op="comment_issue",
        repo=payload.target_repo,
        issue_number=payload.issue_number,
        body=body,
    )


def create_proposed_issues(
    mcp_client: MCPClient,
    *,
    payload: ProposerInput,
    proposal: Proposal,
) -> list[str]:
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
            mcp_client,
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
            pr_url=pr_url,
        ),
    )
    publish(envelope)
    if spawned:
        logger.info(
            "research run completed with spawned issues",
            run_id=payload.run_id,
            spawned_count=len(spawned),
        )


def open_proposal_pr(
    mcp_client: MCPClient,
    *,
    payload: ProposerInput,
    proposal: Proposal,
) -> str:
    """Create a branch, commit edits, open a PR — return the PR URL."""
    branch = branch_name(run_id=payload.run_id)
    invoke_repo_helper(
        mcp_client,
        op="create_branch",
        repo=payload.target_repo,
        branch=branch,
        base=payload.base_branch,
    )
    invoke_repo_helper(
        mcp_client,
        op="commit_files",
        repo=payload.target_repo,
        branch=branch,
        message=f"proposer: {proposal.pr_title}",
        files=[edit_to_dict(edit) for edit in proposal.edits],
    )
    out = invoke_repo_helper(
        mcp_client,
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


def invoke_repo_helper(
    mcp_client: MCPClient,
    *,
    op: str,
    **fields: Any,
) -> dict[str, Any]:
    """Invoke the repo_helper gateway target with one op + raise on error envelope.

    Returns the Lambda's response envelope (``{"ok": True, "op": ...,
    "result": {...}}``). The MCP server serializes dict tool returns
    into both ``structuredContent`` (the raw dict) and ``content[0].text``
    (a JSON string of the same dict); we prefer the structured form and
    fall back to parsing the text block.
    """
    result = call_gateway_tool(
        mcp_client,
        name="repo_helper",
        arguments={"op": op, **fields},
    )
    envelope = extract_envelope(result)
    if not envelope.get("ok"):
        msg = f"repo_helper.{op} failed: {envelope!r}"
        raise RuntimeError(msg)
    return envelope


if __name__ == "__main__":
    app.run()
