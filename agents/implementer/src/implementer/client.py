"""Drives one Claude Agent SDK invocation for a single task.

Flow per invocation:

  1. ``clone_repo`` — pull the project repo into ``/workspace/repo``.
  2. ``fetch_spec`` — download the spec bundle from S3 into ``/workspace/spec``.
  3. ``create_branch`` — branch off ``main`` for this task.
  4. ``ClaudeSDKClient`` — feed Claude the spec + task and let it edit the
     repo via Read/Write/Edit/Bash. Hooks deny dangerous commands.
     The agent ends the session by calling the in-process ``finish`` MCP
     tool with a :class:`FinishReport`; that report drives the PR body.
  5. After Claude finishes, ``mark_done`` flips the tasks.md checkbox.
  6. Commit, push, open PR; return the PR URL + diff summary.
"""

from __future__ import annotations

import structlog
from claude_agent_sdk import ClaudeSDKClient, ResultMessage

from common.runtime import ImplementerInput, ImplementerResult
from implementer.finish import FinishReport, FinishSink
from implementer.options import build_options
from implementer.repo_ops import (
    clone_repo,
    commit_changes,
    create_branch,
    fetch_spec,
    make_session,
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
    target_repo = resolve_target_repo(payload)
    session = make_session(target_repo=target_repo, requestor_sub=payload.requestor_sub)
    logger.info(
        "implementer session opened",
        run_id=payload.run_id,
        target_repo=session.target_repo,
        author_login=session.author_login,
        on_behalf_of_user=session.on_behalf_of_user,
    )

    clone_repo(session)
    fetch_spec(payload.spec_s3_prefix)

    tasks_md = (spec_path() / "tasks.md").read_text(encoding="utf-8")
    task = find_task(parse_tasks(tasks_md), payload.task_id)
    if task is None:
        msg = f"task_id={payload.task_id!r} not found in {payload.spec_s3_prefix}tasks.md"
        raise KeyError(msg)

    branch = task_branch_name(payload.task_id, payload.spec_slug)
    create_branch(branch)

    user_prompt = compose_prompt(payload, task_title=task.title, task_done_when=task.done_when)
    report = await drive_agent(user_prompt, run_id=payload.run_id)

    if report is None:
        logger.warning(
            "implementer ended without calling finish",
            run_id=payload.run_id,
            task_id=payload.task_id,
        )
    elif report.status == "blocked":
        logger.info(
            "implementer reported blocked",
            run_id=payload.run_id,
            task_id=payload.task_id,
            blocked_reason=report.blocked_reason,
        )
        return ImplementerResult(
            task_id=payload.task_id,
            pr_url=None,
            diff_summary="(no diff — task blocked by agent)",
            session_id=payload.run_id,
            blocked_reason=report.blocked_reason,
        )

    materialize_spec_in_repo(payload.spec_slug)
    update_tasks_md(payload.task_id, payload.spec_slug)

    commit_msg = build_commit_message(payload.task_id, task.title)
    commit_changes(commit_msg)
    push_branch(branch)

    pr_url = open_pr(
        session,
        branch=branch,
        base="main",
        title=f"{payload.task_id}: {task.title}",
        body=render_pr_body(payload, task_title=task.title, report=report),
    )

    return ImplementerResult(
        task_id=payload.task_id,
        pr_url=pr_url,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
    )


def resolve_target_repo(payload: ImplementerInput) -> str:
    """Pick the repo this run targets.

    Step Functions / the dashboard always thread ``target_repo`` through
    the agent input (Phase 11a), so the only failure mode is a
    malformed input.
    """
    if payload.target_repo:
        return payload.target_repo
    msg = "ImplementerInput.target_repo is required but missing"
    raise RuntimeError(msg)


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


async def drive_agent(user_prompt: str, *, run_id: str) -> FinishReport | None:
    """Run one ClaudeSDKClient session and return the agent's structured report.

    The ``finish`` MCP tool stashes its validated payload into
    :class:`FinishSink`; this function reads it back after the SDK loop
    drains. Returns ``None`` if the agent ended without calling
    ``finish`` — the caller surfaces that as a fallback PR body and
    emits a warning log.
    """
    sink = FinishSink()
    options = build_options(run_id, finish_sink=sink)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                logger.info(
                    "session done",
                    session_id=msg.session_id,
                    cost_usd=getattr(msg, "total_cost_usd", None),
                )
    return sink.report


def materialize_spec_in_repo(spec_slug: str) -> None:
    """Copy the in-memory spec bundle into ``docs/specs/<slug>/`` in the repo.

    The Architect uploads the bundle to S3 only; the implementer is the
    first agent that touches the project repo, so it's responsible for
    materializing the spec there. Idempotent — overwrites existing files
    so the latest tasks.md state (with prior checkboxes) propagates.
    """
    target = repo_path() / "docs" / "specs" / spec_slug
    target.mkdir(parents=True, exist_ok=True)
    for doc in ("requirements", "design", "tasks"):
        src = spec_path() / f"{doc}.md"
        if src.exists():
            (target / f"{doc}.md").write_bytes(src.read_bytes())


def update_tasks_md(task_id: str, spec_slug: str) -> None:
    """Flip the task's checkbox in the repo's copy of tasks.md.

    Idempotent — if the row is already ``[x]`` (e.g. on a retry that
    reuses the workspace) we leave the file untouched.
    """
    target = repo_path() / "docs" / "specs" / spec_slug / "tasks.md"
    body = target.read_text(encoding="utf-8")
    try:
        target.write_text(mark_done(body, task_id), encoding="utf-8")
    except KeyError:
        return


def build_commit_message(task_id: str, title: str) -> str:
    """Imperative one-line commit subject."""
    return f"{task_id}: {title}"


def render_pr_body(
    payload: ImplementerInput,
    *,
    task_title: str,
    report: FinishReport | None,
) -> str:
    """Render the PR body from a :class:`FinishReport`.

    Sections (in order, omitted when empty): Summary, Files changed,
    Tests, Risks. Always emits a footer with run + correlation IDs and
    a link to the in-repo spec folder. If ``report`` is ``None``, emits
    a fallback body explaining that ``finish`` was not called.
    """
    if report is None:
        return render_no_finish_body(payload, task_title=task_title)

    lines = [
        f"## {payload.task_id}: {task_title}",
        "",
        "### Summary",
        "",
        report.summary,
        "",
    ]
    if report.files_changed:
        lines += ["### Files changed", ""]
        lines += [f"- `{path}`" for path in report.files_changed]
        lines += [""]
    if report.tests_run:
        lines += ["### Tests", ""]
        lines += [f"- `{t.name}` — {t.status}" for t in report.tests_run]
        lines += [""]
    if report.risks:
        lines += ["### Risks", ""]
        lines += [f"- {risk}" for risk in report.risks]
        lines += [""]
    lines += [
        "---",
        (
            f"_run_id: {payload.run_id}_  ·  "
            f"_correlation_id: {payload.correlation_id}_  ·  "
            f"_spec: `docs/specs/{payload.spec_slug}/`_"
        ),
    ]
    return "\n".join(lines)


def render_no_finish_body(payload: ImplementerInput, *, task_title: str) -> str:
    """Fallback PR body when the agent never called ``finish``."""
    return (
        f"## {payload.task_id}: {task_title}\n\n"
        "_Implementer ended the session without calling the `finish` tool — "
        "no structured summary is available. See the diff for details._\n\n"
        f"---\n"
        f"_run_id: {payload.run_id}_  ·  _correlation_id: {payload.correlation_id}_  ·  "
        f"_spec: `docs/specs/{payload.spec_slug}/`_"
    )
