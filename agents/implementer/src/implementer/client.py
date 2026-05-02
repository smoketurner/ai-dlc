"""Drives one Claude Agent SDK invocation for a single task.

Flow per invocation:

  1. ``clone_repo`` — pull the project repo into ``/workspace/repo``.
  2. ``fetch_spec`` — download the spec bundle from S3 into ``/workspace/spec``.
  3. ``create_branch`` — branch off ``main`` for this task.
  4. ``ClaudeSDKClient`` — feed Claude the spec + task and let it edit the
     repo via Read/Write/Edit/Bash. Hooks deny dangerous commands.
  5. After Claude finishes, ``mark_done`` flips the tasks.md checkbox.
  6. Commit, push, open PR; return the PR URL + diff summary.
"""

from __future__ import annotations

import structlog
from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock

from common.runtime import ImplementerInput, ImplementerResult
from implementer.options import build_options
from implementer.repo_ops import (
    clone_repo,
    commit_changes,
    create_branch,
    fetch_spec,
    open_pr,
    push_branch,
    repo_path,
    short_diff_summary,
    spec_path,
    task_branch_name,
)
from implementer.tasks import find_task, mark_done, parse_tasks

logger = structlog.get_logger()


async def execute_task(payload: ImplementerInput) -> ImplementerResult:
    """Run one task end-to-end and return the SPEC.READY-style result."""
    clone_repo()
    fetch_spec(payload.spec_s3_prefix)

    tasks_md = (spec_path() / "tasks.md").read_text(encoding="utf-8")
    task = find_task(parse_tasks(tasks_md), payload.task_id)
    if task is None:
        msg = f"task_id={payload.task_id!r} not found in {payload.spec_s3_prefix}tasks.md"
        raise KeyError(msg)

    branch = task_branch_name(payload.task_id, payload.spec_slug)
    create_branch(branch)

    user_prompt = compose_prompt(payload, task_title=task.title, task_done_when=task.done_when)
    assistant_text = await drive_agent(user_prompt)

    update_tasks_md(payload.task_id)

    commit_msg = build_commit_message(payload.task_id, task.title)
    commit_changes(commit_msg)
    push_branch(branch)

    pr_url = open_pr(
        branch=branch,
        base="main",
        title=f"{payload.task_id}: {task.title}",
        body=build_pr_body(payload, task_title=task.title, assistant_text=assistant_text),
    )

    return ImplementerResult(
        task_id=payload.task_id,
        pr_url=pr_url,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
    )


def compose_prompt(
    payload: ImplementerInput, *, task_title: str, task_done_when: str | None
) -> str:
    """Compose the user message handed to Claude."""
    parts = [
        f"Spec: {payload.spec_slug}  (files in /workspace/spec/)",
        f"Project: {payload.project_slug}  (repo at /workspace/repo/)",
        f"Task: {payload.task_id} — {task_title}",
    ]
    if task_done_when:
        parts.append(f"Done when: {task_done_when}")
    if payload.prior_feedback:
        parts += [
            "",
            "Reviewer rejected the prior PR for this task. Address every point:",
            payload.prior_feedback.strip(),
        ]
    parts += [
        "",
        "Read /workspace/spec/requirements.md and /workspace/spec/design.md before "
        "you start. Make the smallest set of edits that satisfies this task's "
        "acceptance criteria. Run lint/format/type/test before you stop.",
    ]
    return "\n".join(parts)


async def drive_agent(user_prompt: str) -> str:
    """Run one ClaudeSDKClient session and return the concatenated assistant text."""
    options = build_options()
    text_blocks: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_blocks.append(block.text)
            elif isinstance(msg, ResultMessage):
                logger.info(
                    "session done",
                    session_id=msg.session_id,
                    cost_usd=getattr(msg, "total_cost_usd", None),
                )
    return "\n\n".join(text_blocks)


def update_tasks_md(task_id: str) -> None:
    """Flip the task's checkbox in the repo's local copy of tasks.md."""
    # The architect commits tasks.md to docs/specs/{slug}/tasks.md; the local
    # spec copy under /workspace/spec is read-only context. Update the repo
    # version so the commit reflects the completed task.
    candidate = repo_path() / "docs" / "specs"
    for p in candidate.rglob("tasks.md"):
        body = p.read_text(encoding="utf-8")
        try:
            updated = mark_done(body, task_id)
        except KeyError:
            continue
        p.write_text(updated, encoding="utf-8")
        return


def build_commit_message(task_id: str, title: str) -> str:
    """Imperative one-line commit subject."""
    return f"{task_id}: {title}"


def build_pr_body(payload: ImplementerInput, *, task_title: str, assistant_text: str) -> str:
    """Markdown body for the PR — links spec, task, summary."""
    return (
        f"## {payload.task_id}: {task_title}\n\n"
        f"Implements one task from spec `{payload.spec_slug}`.\n\n"
        f"### Implementer notes\n\n"
        f"{assistant_text}\n\n"
        f"---\n"
        f"_run_id: {payload.run_id}_  ·  _correlation_id: {payload.correlation_id}_"
    )
