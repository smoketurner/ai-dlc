"""AgentCore Runtime entrypoint for the Retrospector.

The dispatcher Lambda invokes this runtime once per terminal event
(``RUN.COMPLETED`` / ``RUN.FAILED`` / ``RUN.CANCEL_REQUESTED``). The
entrypoint:

  1. Validates the input as :class:`RetrospectorInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the synthesis runs.
  3. Spawns a daemon thread under a copied :class:`contextvars.Context`
     that asks the Strands agent for a :class:`RetrospectiveDecision`.
     If the decision proposes a lesson, opens a PR via the
     gateway-routed ``repo_helper`` that appends the addition to either
     ``MEMORY.md`` (structured six-section schema; root preferred,
     ``docs/`` fallback for legacy projects) or ``AGENTS.md``
     (free-form append, root only).
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms.

Memory reads go through ``repo_helper.get_file`` against ``main`` so
the repo is the source of truth — no S3 mirror lag, no duplicate
bullets after rapid retrospective fires.

``contextvars.copy_context()`` carries the runtime's
``WorkloadAccessToken`` ContextVar into the daemon thread so
:func:`common.gateway_tools.fetch_gateway_token` can exchange it for a
Cognito M2M JWT via AgentCore Identity.

Retrospectives never advance the run state machine — they're a
side-channel learner. Failures are logged and swallowed; the system
continues to function without retrospectives if the agent or
``repo_helper`` is unavailable.
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from common.gateway_tools import call_gateway_tool, gateway_mcp_client
from common.memory_md import MemoryDoc, parse, render
from common.runtime import RetrospectorInput
from retrospector.agent import build_agent, retrospect
from retrospector.decision import RetrospectiveDecision

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

MEMORY_MD_CANDIDATES = ("MEMORY.md", "docs/MEMORY.md")
AGENTS_MD_PATH = "AGENTS.md"
RUN_ID_BRANCH_RE = re.compile(r"[^a-z0-9-]+")


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = RetrospectorInput.model_validate(event)
    logger.info(
        "retrospector invoked",
        run_id=payload.run_id,
        event_type=payload.event_type,
        target_repo=payload.target_repo,
    )
    task_id = app.add_async_task("retrospector_run", {"run_id": payload.run_id})
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_retrospective, payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_retrospective(payload: RetrospectorInput, async_task_id: int) -> None:
    """Body of the retrospective — ask the agent, optionally open a memory-file PR."""
    try:
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent = build_agent(payload.run_id, mcp_client=mcp_client)
            decision = retrospect(
                agent,
                event_type=payload.event_type,
                project_slug=payload.project_slug,
                target_repo=payload.target_repo,
                pr_url=payload.pr_url or None,
                issue_url=payload.issue_url or None,
                reason=payload.reason or None,
                revision_count=payload.revision_count,
                validation_artifact_keys=tuple(payload.validation_artifact_keys),
            )
            if not decision.has_lesson:
                logger.info(
                    "retrospective: no lesson",
                    run_id=payload.run_id,
                    event_type=payload.event_type,
                    rationale=decision.rationale[:200],
                )
                return
            pr_url = open_memory_pr(mcp_client, payload=payload, decision=decision)
            logger.info(
                "retrospective: lesson recorded",
                run_id=payload.run_id,
                event_type=payload.event_type,
                target_file=decision.target_file,
                confidence=decision.confidence,
                pr_url=pr_url,
            )
    except Exception:
        logger.exception("retrospective failed", run_id=payload.run_id)
    finally:
        app.complete_async_task(async_task_id)


def open_memory_pr(
    mcp_client: MCPClient,
    *,
    payload: RetrospectorInput,
    decision: RetrospectiveDecision,
) -> str:
    """Append ``decision.memory_md_addition`` to the chosen file and open a PR.

    Reads the current file from ``main`` via the gateway-routed
    ``repo_helper.get_file`` (the repo is the source of truth — no S3
    staleness). For ``MEMORY.md`` the body is parsed against the
    six-section schema and the addition lands under
    ``decision.section``; for ``AGENTS.md`` the addition is appended
    verbatim at the end of the file. The result is committed to a
    fresh branch and a PR is opened against ``main``.
    """
    if decision.target_file is None:
        msg = "decision.target_file must be set when opening a memory-file PR"
        raise ValueError(msg)
    branch = branch_name(run_id=payload.run_id)
    if decision.target_file == "MEMORY.md":
        path, existing = resolve_memory_path(
            mcp_client,
            repo=payload.target_repo,
            candidates=MEMORY_MD_CANDIDATES,
        )
    else:
        path = AGENTS_MD_PATH
        existing = fetch_file(mcp_client, repo=payload.target_repo, path=path)
    invoke_repo_helper(
        mcp_client,
        op="create_branch",
        repo=payload.target_repo,
        branch=branch,
        base="main",
    )
    new_content = render_patch(
        existing=existing,
        decision=decision,
    )
    invoke_repo_helper(
        mcp_client,
        op="commit_files",
        repo=payload.target_repo,
        branch=branch,
        message=render_commit_message(decision),
        files=[{"path": path, "content": new_content}],
    )
    out = invoke_repo_helper(
        mcp_client,
        op="open_pr",
        repo=payload.target_repo,
        base="main",
        head=branch,
        title=render_pr_title(decision),
        body=render_pr_body(payload=payload, decision=decision),
    )
    pr_url = out.get("result", {}).get("pr_url")
    if not isinstance(pr_url, str):
        msg = f"open_pr did not return a pr_url: {out!r}"
        raise TypeError(msg)
    return pr_url


def fetch_file(mcp_client: MCPClient, *, repo: str, path: str) -> str:
    """Read ``path`` from ``main`` via the gateway-routed ``repo_helper.get_file``.

    Returns the empty string when the file doesn't exist (e.g., the
    project hasn't created an ``AGENTS.md`` yet). Callers handle the
    empty-existing case by seeding a default body.
    """
    out = invoke_repo_helper(mcp_client, op="get_file", repo=repo, path=path, ref="main")
    result = out.get("result") or {}
    if not result.get("exists"):
        return ""
    return str(result.get("content", ""))


def resolve_memory_path(
    mcp_client: MCPClient,
    *,
    repo: str,
    candidates: tuple[str, ...],
) -> tuple[str, str]:
    """Probe ``candidates`` in order and return ``(path, existing_body)``.

    Used by :func:`open_memory_pr` to pick the right location for the
    project's memory file. Preference: first candidate (typically root)
    when the file exists there; ``docs/`` fallback when only that is
    present; first candidate again as the default for projects that have
    no memory file yet (so new repos get the modern root-level layout).
    """
    for path in candidates:
        body = fetch_file(mcp_client, repo=repo, path=path)
        if body:
            return path, body
    return candidates[0], ""


def render_patch(*, existing: str, decision: RetrospectiveDecision) -> str:
    """Produce the new file content for whichever target_file was chosen."""
    if decision.target_file == "MEMORY.md":
        if decision.section is None:
            msg = "MEMORY.md target requires a section"
            raise ValueError(msg)
        doc = parse(existing) if existing.strip() else MemoryDoc()
        return render(doc.with_appended(decision.section, decision.memory_md_addition.strip()))
    return render_agents_md_patch(existing=existing, addition=decision.memory_md_addition)


def render_agents_md_patch(*, existing: str, addition: str) -> str:
    """Append ``addition`` to ``AGENTS.md`` content (free-form Markdown).

    Adds a blank line between the existing body and the addition so the
    new content reads as a fresh paragraph / section. When the file
    is empty (no existing AGENTS.md), seeds it with a minimal title.
    """
    body = existing.rstrip("\n")
    add = addition.strip()
    if not body:
        return f"# Project Memory\n\n{add}\n"
    return f"{body}\n\n{add}\n"


def render_commit_message(decision: RetrospectiveDecision) -> str:
    """Single-line commit message — uses the lesson summary."""
    summary = decision.lesson_summary.strip().splitlines()[0][:72]
    return f"retrospective: {summary}"


def render_pr_title(decision: RetrospectiveDecision) -> str:
    """PR title — ≤72 chars, leads with the agent identifier."""
    summary = decision.lesson_summary.strip().splitlines()[0]
    title = f"retrospective: {summary}"
    return title[:72]


def render_pr_body(
    *,
    payload: RetrospectorInput,
    decision: RetrospectiveDecision,
) -> str:
    """PR body — quotes the agent's rationale and links the source event."""
    parts = [
        f"Recorded a lesson from {payload.event_type} on this run.",
        "",
        f"**Target file:** `{decision.target_file}`"
        + (f" (section `{decision.section}`)" if decision.section else ""),
        "",
        "**Lesson:**",
        decision.lesson_summary,
        "",
        "**Rationale:**",
        decision.rationale,
        "",
        f"Confidence: {decision.confidence:.2f}",
    ]
    if payload.pr_url:
        parts.extend(["", f"Source PR: {payload.pr_url}"])
    if payload.issue_url:
        parts.extend(["", f"Source issue: {payload.issue_url}"])
    return "\n".join(parts) + "\n"


def branch_name(*, run_id: str) -> str:
    """Deterministic branch name per run — keeps re-fires idempotent on the remote."""
    safe = RUN_ID_BRANCH_RE.sub("-", run_id.lower())
    return f"retrospective/{safe}"


def extract_envelope(result: Any) -> dict[str, Any]:
    """Pull the Lambda return envelope out of an MCPToolResult.

    The MCP server serializes dict tool returns into both
    ``structuredContent`` (the raw dict) and ``content[0].text`` (a
    JSON string of the same dict); prefer the structured form and fall
    back to parsing the text block.
    """
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if isinstance(structured, dict):
        return structured
    blocks = result.get("content", []) if isinstance(result, dict) else []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    msg = f"repo_helper returned no parseable content: {result!r}"
    raise RuntimeError(msg)


def invoke_repo_helper(
    mcp_client: MCPClient,
    *,
    op: str,
    **fields: Any,
) -> dict[str, Any]:
    """Invoke the gateway-routed ``repo_helper`` target and raise on error envelope.

    Returns the full envelope (``{"ok": True, "op": ..., "result":
    {...}}``) so callers can pull from ``result`` without losing the
    envelope shape that older tests assert against.
    """
    response = call_gateway_tool(
        mcp_client,
        name="repo_helper",
        arguments={"op": op, **fields},
    )
    envelope = extract_envelope(response)
    if not envelope.get("ok"):
        msg = f"repo_helper.{op} failed: {envelope!r}"
        raise RuntimeError(msg)
    return envelope


if __name__ == "__main__":
    app.run()
