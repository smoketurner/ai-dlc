"""AgentCore Runtime entrypoint for the Retrospector.

The dispatcher invokes this in one of two modes:

* ``mode="capture"`` — fired on every PR-signal event (terminal,
  validator verdict, CI state, ``@aidlc-bot`` mention). The agent
  emits a :class:`CaptureDecision` containing zero or more
  :class:`LessonBullet` records; each bullet is written as one
  short-term event in AgentCore Memory under a stable
  ``(actor_id, session_id)`` per destination. **No PR is opened.**
* ``mode="consolidate"`` — fired by a weekly scheduled rule, fanned
  out one invocation per destination (per active project for
  ``target_repo``, once for ``platform``). The agent reads every
  pending event for the destination's ``(actor_id, session_id)``,
  emits a :class:`ConsolidationPlan` with the patches to ship plus
  the event IDs to delete; this module opens up to two PRs
  (MEMORY.md additions, SKILL.md files) via ``repo_helper`` and
  deletes the shipped + discarded events. Anything left over is
  deferred automatically — no buffer to re-render.

The dispatcher Lambda invokes synchronously; we hand off to a
daemon thread so the HTTP response returns immediately. Failures
are logged and swallowed — the platform never wedges on the
Retrospector's absence.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import re
import threading
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from common.agentcore_memory import (
    MemoryEvent,
    StoredEvent,
    create_event,
    delete_event,
    list_events,
)
from common.gateway_tools import (
    REPO_HELPER,
    call_gateway_tool,
    extract_envelope,
    gateway_mcp_client,
)
from common.memory_md import MemoryDoc, parse, render
from common.runtime import RetrospectorInput
from retrospector.agent import build_agent, capture, consolidate
from retrospector.decision import (
    CaptureDecision,
    ConsolidationPlan,
    LessonBullet,
    MemoryAddition,
    SkillFile,
)

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

RETROSPECTOR_ACTOR = "retrospector"
SAFE_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = RetrospectorInput.model_validate(event)
    logger.info(
        "retrospector invoked",
        mode=payload.mode,
        run_id=payload.run_id,
        event_type=payload.event_type,
        destination=payload.destination,
        target_repo=payload.target_repo,
    )
    task_id = app.add_async_task("retrospector_run", {"run_id": payload.run_id})
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(dispatch_by_mode, payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def dispatch_by_mode(payload: RetrospectorInput, async_task_id: int) -> None:
    """Build the agent for the right mode and run the corresponding flow."""
    try:
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent = build_agent(payload.run_id, mode=payload.mode, mcp_client=mcp_client)
            if payload.mode == "capture":
                run_capture(agent, payload=payload)
            else:
                run_consolidate(agent, payload=payload, mcp_client=mcp_client)
    except Exception:
        logger.exception(
            "retrospective failed",
            mode=payload.mode,
            run_id=payload.run_id,
        )
    finally:
        app.complete_async_task(async_task_id)


def run_capture(agent: Any, *, payload: RetrospectorInput) -> None:
    """Ask the agent for lesson bullets and write each as one memory event."""
    decision = capture(
        agent,
        event_type=payload.event_type,  # ty: ignore[invalid-argument-type]
        project_slug=payload.project_slug,
        target_repo=payload.target_repo,
        pr_url=payload.pr_url or None,
        issue_url=payload.issue_url or None,
        reason=payload.reason or None,
        verdict=payload.verdict,
        pr_comment_body=payload.pr_comment_body or None,
        revision_count=payload.revision_count,
        validation_artifact_keys=tuple(payload.validation_artifact_keys),
    )
    if not decision.bullets:
        logger.info(
            "capture: no bullets",
            run_id=payload.run_id,
            event_type=payload.event_type,
            rationale=decision.rationale[:200],
        )
        return
    counts = write_bullets(bullets=decision.bullets, payload=payload)
    logger.info(
        "capture: bullets written",
        run_id=payload.run_id,
        event_type=payload.event_type,
        counts=counts,
    )


def run_consolidate(
    agent: Any,
    *,
    payload: RetrospectorInput,
    mcp_client: MCPClient,
) -> None:
    """List pending events, run the agent, open PRs, delete shipped + discarded."""
    if payload.destination is None:
        msg = "consolidate mode requires destination on the input"
        raise ValueError(msg)
    session = session_id_for(
        destination=payload.destination,
        project_slug=payload.project_slug,
    )
    events = list_events(
        agentcore_client(),
        memory_id=memory_id(),
        actor_id=RETROSPECTOR_ACTOR,
        session_id=session,
    )
    if not events:
        logger.info(
            "consolidate: no pending events",
            destination=payload.destination,
            project_slug=payload.project_slug,
        )
        return
    buffer_content = render_events_as_buffer(events)
    plan = consolidate(
        agent,
        destination=payload.destination,
        project_slug=payload.project_slug,
        target_repo=payload.target_repo,
        buffer_content=buffer_content,
    )
    pr_urls = open_consolidation_prs(mcp_client, payload=payload, plan=plan)
    removed = delete_consumed_events(
        session=session,
        event_ids=[*plan.shipped_event_ids, *plan.discarded_event_ids],
    )
    logger.info(
        "consolidate: completed",
        destination=payload.destination,
        project_slug=payload.project_slug,
        pr_urls=pr_urls,
        memory_count=len(plan.memory_additions),
        skill_count=len(plan.skill_files),
        events_removed=removed,
        events_deferred=len(events) - removed,
    )


def write_bullets(
    *,
    bullets: list[LessonBullet],
    payload: RetrospectorInput,
) -> dict[str, int]:
    """Write one short-term event per bullet under its destination's session."""
    client = agentcore_client()
    counts: dict[str, int] = {}
    for bullet in bullets:
        session = session_id_for(
            destination=bullet.destination,
            project_slug=payload.project_slug,
        )
        text = json.dumps(
            {
                "run_id": payload.run_id,
                "event_type": payload.event_type,
                "verdict": payload.verdict,
                "bullet": bullet.model_dump(),
            },
            sort_keys=True,
        )
        create_event(
            client,
            memory_id=memory_id(),
            actor_id=RETROSPECTOR_ACTOR,
            session_id=session,
            events=[MemoryEvent(role="TOOL", text=text)],
        )
        counts[bullet.destination] = counts.get(bullet.destination, 0) + 1
    return counts


def session_id_for(*, destination: str, project_slug: str) -> str:
    """Return the AgentCore Memory session id for the destination's buffer."""
    if destination == "platform":
        return "pending_lessons:platform"
    safe = SAFE_SLUG_RE.sub("-", project_slug.lower()).strip("-") or "unknown"
    return f"pending_lessons:target:{safe}"


def render_events_as_buffer(events: list[StoredEvent]) -> str:
    """Render stored events as a Markdown buffer for the consolidate prompt.

    Each entry leads with the event ID (so the agent can reference it in
    ``shipped_event_ids`` / ``discarded_event_ids``) and includes the
    capture context + bullet JSON.
    """
    blocks: list[str] = []
    for event in sorted(events, key=lambda evt: evt.timestamp):
        ts = event.timestamp.isoformat(timespec="seconds")
        blocks.append(
            f"## event_id={event.event_id} — {ts}\n\n```json\n{event.text}\n```",
        )
    return "\n\n".join(blocks)


def delete_consumed_events(*, session: str, event_ids: list[str]) -> int:
    """Delete every shipped + discarded event; return the count actually deleted."""
    if not event_ids:
        return 0
    client = agentcore_client()
    removed = 0
    for event_id in event_ids:
        try:
            delete_event(
                client,
                memory_id=memory_id(),
                actor_id=RETROSPECTOR_ACTOR,
                session_id=session,
                event_id=event_id,
            )
        except Exception:
            logger.exception("delete_event failed", session=session, event_id=event_id)
            continue
        removed += 1
    return removed


def open_consolidation_prs(
    mcp_client: MCPClient,
    *,
    payload: RetrospectorInput,
    plan: ConsolidationPlan,
) -> list[str]:
    """Open up to two PRs: one for MEMORY.md additions, one for SKILL.md files."""
    pr_urls: list[str] = []
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    if plan.memory_additions:
        memory_files = memory_files_for(
            mcp_client,
            payload=payload,
            additions=plan.memory_additions,
        )
        pr_urls.append(
            commit_and_open_pr(
                mcp_client,
                payload=payload,
                plan=plan,
                branch=branch_name(payload=payload, kind="memory", timestamp=timestamp),
                files=memory_files,
                title_kind="memory",
            ),
        )
    if plan.skill_files:
        skill_files = [
            {"path": f"{skill.scope}/SKILL.md", "content": render_skill_file(skill)}
            for skill in plan.skill_files
        ]
        pr_urls.append(
            commit_and_open_pr(
                mcp_client,
                payload=payload,
                plan=plan,
                branch=branch_name(payload=payload, kind="skills", timestamp=timestamp),
                files=skill_files,
                title_kind="skills",
            ),
        )
    return pr_urls


def memory_files_for(
    mcp_client: MCPClient,
    *,
    payload: RetrospectorInput,
    additions: list[MemoryAddition],
) -> list[dict[str, str]]:
    """Read each affected MEMORY.md, append per section, return the new file contents."""
    by_scope: dict[str, list[MemoryAddition]] = {}
    for addition in additions:
        by_scope.setdefault(addition.scope, []).append(addition)
    files: list[dict[str, str]] = []
    for scope, scope_additions in by_scope.items():
        existing = fetch_file(mcp_client, repo=payload.target_repo, path=scope)
        doc = parse(existing) if existing.strip() else MemoryDoc()
        for addition in scope_additions:
            doc = doc.with_appended(addition.section, addition.addition.strip())
        files.append({"path": scope, "content": render(doc)})
    return files


def render_skill_file(skill: SkillFile) -> str:
    """Render a SkillFile as the agentskills.io-shaped Markdown body."""
    frontmatter = f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n"
    return frontmatter + skill.body.rstrip() + "\n"


def commit_and_open_pr(
    mcp_client: MCPClient,
    *,
    payload: RetrospectorInput,
    plan: ConsolidationPlan,
    branch: str,
    files: list[dict[str, str]],
    title_kind: str,
) -> str:
    """Create the branch, commit ``files`` in one commit, open a PR, return its URL."""
    invoke_repo_helper(
        mcp_client,
        op="create_branch",
        repo=payload.target_repo,
        branch=branch,
        base="main",
    )
    invoke_repo_helper(
        mcp_client,
        op="commit_files",
        repo=payload.target_repo,
        branch=branch,
        message=f"retrospective ({title_kind}): batch consolidation",
        files=files,
    )
    out = invoke_repo_helper(
        mcp_client,
        op="open_pr",
        repo=payload.target_repo,
        base="main",
        head=branch,
        title=f"retrospective ({title_kind}): batch consolidation",
        body=render_pr_body(payload=payload, plan=plan, title_kind=title_kind),
    )
    pr_url = out.get("result", {}).get("pr_url")
    if not isinstance(pr_url, str):
        msg = f"open_pr did not return a pr_url: {out!r}"
        raise TypeError(msg)
    return pr_url


def render_pr_body(
    *,
    payload: RetrospectorInput,
    plan: ConsolidationPlan,
    title_kind: str,
) -> str:
    """PR body — quotes the consolidator's rationale and lists what was shipped."""
    parts = [
        f"Batch consolidation for **{payload.destination}** "
        f"({title_kind}). Run by the Retrospector on {dt.datetime.now(dt.UTC).date().isoformat()}.",
        "",
        "**Rationale:**",
        plan.rationale,
    ]
    if title_kind == "memory" and plan.memory_additions:
        parts += ["", "**MEMORY.md additions:**"]
        for addition in plan.memory_additions:
            parts.append(f"- `{addition.scope}` → `{addition.section}`")
    if title_kind == "skills" and plan.skill_files:
        parts += ["", "**SKILL.md files:**"]
        for skill in plan.skill_files:
            parts.append(f"- `{skill.scope}/SKILL.md` — {skill.description}")
    return "\n".join(parts) + "\n"


def branch_name(*, payload: RetrospectorInput, kind: str, timestamp: str) -> str:
    """Deterministic branch name per consolidation batch."""
    dest = payload.destination or "unknown"
    return f"retrospective/{dest}/{timestamp}-{kind}"


def fetch_file(mcp_client: MCPClient, *, repo: str, path: str) -> str:
    """Read ``path`` from ``main`` via the gateway-routed ``repo_helper.get_file``."""
    out = invoke_repo_helper(mcp_client, op="get_file", repo=repo, path=path, ref="main")
    result = out.get("result") or {}
    if not result.get("exists"):
        return ""
    return str(result.get("content", ""))


def invoke_repo_helper(
    mcp_client: MCPClient,
    *,
    op: str,
    **fields: Any,
) -> dict[str, Any]:
    """Invoke the gateway-routed ``repo_helper`` target and raise on error envelope."""
    response = call_gateway_tool(
        mcp_client,
        name=REPO_HELPER,
        arguments={"op": op, **fields},
    )
    envelope = extract_envelope(response)
    if not envelope.get("ok"):
        msg = f"repo_helper.{op} failed: {envelope!r}"
        raise RuntimeError(msg)
    return envelope


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached AgentCore data-plane client (memory ops)."""
    return boto3.client("bedrock-agentcore")


def memory_id() -> str:
    """AgentCore Memory resource id, supplied at deploy via env var."""
    return os.environ["AIDLC_MEMORY_ID"]


# Re-export the structured-output types so dispatcher and tests can import
# from this module without reaching across packages.
__all__ = [
    "CaptureDecision",
    "ConsolidationPlan",
    "LessonBullet",
    "MemoryAddition",
    "SkillFile",
]
