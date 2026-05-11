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

from typing import Any

import structlog
from claude_agent_sdk import ClaudeSDKClient, ResultMessage

from common.memory import agent_memory_preamble
from common.runtime import (
    CiFailureFeedback,
    FeedbackItem,
    ImplementerInput,
    ImplementerResult,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)
from implementer.finish import FinishReport, FinishSink
from implementer.lint_gate import LintGateResult, compose_lint_feedback, run_lint_gate
from implementer.options import build_options
from implementer.repo_ops import (
    agent_made_real_changes,
    checkout_task_branch,
    clone_repo,
    commit_changes,
    create_branch,
    fetch_failed_check_runs,
    fetch_spec,
    has_uncommitted_changes,
    make_session,
    open_pr,
    parse_pr_number,
    post_inline_replies,
    push_branch,
    repo_path,
    run_git,
    short_diff_summary,
    spec_path,
    task_branch_name,
)
from implementer.tasks import find_task, mark_done, parse_tasks

logger = structlog.get_logger()


async def execute_task(payload: ImplementerInput) -> ImplementerResult:
    """Dispatch to the iteration vs initial flow based on ``iteration_count``."""
    if payload.iteration_count > 0:
        return await execute_iteration(payload)
    return await execute_initial(payload)


async def execute_initial(payload: ImplementerInput) -> ImplementerResult:
    """Bootstrap flow: clone main, create task branch, agent edits, open PR."""
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

    branch = task_branch_name(payload.task_id, payload.spec_slug, payload.run_id)
    create_branch(branch)

    user_prompt = compose_prompt(payload, task_title=task.title, task_done_when=task.done_when)
    report, usage = await drive_agent(user_prompt, run_id=payload.run_id)

    blocked_reason = compute_blocked_reason(payload, report)

    gate_result: LintGateResult | None = None
    if blocked_reason is None:
        gate_result, report, extra_usage = await run_lint_gate_with_retry(
            report=report,
            usage=usage,
            run_id=payload.run_id,
        )
        usage = merge_usage(usage, extra_usage)
        materialize_spec_in_repo(payload.spec_slug)
        update_tasks_md(payload.task_id, payload.spec_slug)
        commit_message = build_commit_message(payload.task_id, task.title)
        body = render_pr_body(payload, task_title=task.title, report=report)
        title = f"{payload.task_id}: {task.title}"
    else:
        write_blocked_md(
            spec_slug=payload.spec_slug,
            task_id=payload.task_id,
            blocked_reason=blocked_reason,
            report=report,
        )
        commit_message = build_blocked_commit_message(payload.task_id, task.title)
        body = render_blocked_pr_body(
            payload,
            task_title=task.title,
            blocked_reason=blocked_reason,
            report=report,
        )
        title = f"{payload.task_id} (blocked): {task.title}"

    if has_uncommitted_changes():
        commit_changes(commit_message)
    push_branch(branch)

    pr_url = open_pr(
        session,
        branch=branch,
        base="main",
        title=title,
        body=body,
        draft=blocked_reason is not None,
    )

    return ImplementerResult(
        task_id=payload.task_id,
        pr_url=pr_url,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
        blocked_reason=blocked_reason,
        lint_gate=gate_result,
        **usage,
    )


async def execute_iteration(payload: ImplementerInput) -> ImplementerResult:
    """Iteration flow: check out existing PR branch, push fix commit, post replies."""
    target_repo = resolve_target_repo(payload)
    session = make_session(target_repo=target_repo, requestor_sub=payload.requestor_sub)
    branch = task_branch_name(payload.task_id, payload.spec_slug, payload.run_id)
    logger.info(
        "implementer iteration session opened",
        run_id=payload.run_id,
        iteration=payload.iteration_count,
        target_repo=session.target_repo,
        branch=branch,
    )

    clone_repo(session)
    checkout_task_branch(branch)
    fetch_spec(payload.spec_s3_prefix)

    tasks_md = (spec_path() / "tasks.md").read_text(encoding="utf-8")
    task = find_task(parse_tasks(tasks_md), payload.task_id)
    if task is None:
        msg = f"task_id={payload.task_id!r} not found in {payload.spec_s3_prefix}tasks.md"
        raise KeyError(msg)

    failed_checks = []
    if any_ci_failure_feedback(payload.iteration_feedback):
        head_sha = run_git("rev-parse", "HEAD").strip()
        failed_checks = fetch_failed_check_runs(
            repo=target_repo, head_sha=head_sha, requestor_sub=payload.requestor_sub
        )

    user_prompt = compose_iteration_prompt(
        payload,
        task_title=task.title,
        task_done_when=task.done_when,
        failed_checks=failed_checks,
    )
    report, usage = await drive_agent(user_prompt, run_id=payload.run_id)

    blocked_reason = compute_blocked_reason(payload, report)

    gate_result: LintGateResult | None = None
    if blocked_reason is None:
        gate_result, report, extra_usage = await run_lint_gate_with_retry(
            report=report,
            usage=usage,
            run_id=payload.run_id,
        )
        usage = merge_usage(usage, extra_usage)
        # Iteration produced a real diff — clean up any prior BLOCKED.md
        # so it doesn't ride along into main when the PR is merged.
        delete_blocked_md(payload.spec_slug)
        commit_message = build_iteration_commit_message(
            payload.task_id,
            task.title,
            payload.iteration_count,
        )
    else:
        write_blocked_md(
            spec_slug=payload.spec_slug,
            task_id=payload.task_id,
            blocked_reason=blocked_reason,
            report=report,
        )
        commit_message = build_blocked_iteration_commit_message(
            payload.task_id,
            task.title,
            payload.iteration_count,
        )

    if has_uncommitted_changes():
        commit_changes(commit_message)
    push_branch(branch)

    if report is not None and report.inline_replies and payload.pr_url is not None:
        post_inline_replies(
            repo=target_repo,
            pr_number=parse_pr_number(payload.pr_url),
            requestor_sub=payload.requestor_sub,
            replies=[(r.comment_id, r.body) for r in report.inline_replies],
        )

    return ImplementerResult(
        task_id=payload.task_id,
        pr_url=payload.pr_url,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
        blocked_reason=blocked_reason,
        lint_gate=gate_result,
        **usage,
    )


def compute_blocked_reason(
    payload: ImplementerInput,
    report: FinishReport | None,
) -> str | None:
    """Return the agent's blocker explanation, or ``None`` on a real diff.

    A blocked path lands when the agent makes no real changes (``status
    --porcelain`` is empty outside the spec dir) or the agent itself
    reported ``status='blocked'`` via the ``finish`` tool. The runtime
    still opens a draft PR carrying ``BLOCKED.md`` so a human can
    advance the task by commenting on the PR.
    """
    if report is None:
        logger.warning(
            "implementer ended without calling finish",
            run_id=payload.run_id,
            task_id=payload.task_id,
        )
    if not agent_made_real_changes(payload.spec_slug):
        reason = (
            report.blocked_reason if report and report.blocked_reason else "agent produced no diff"
        )
        logger.info(
            "implementer produced no diff",
            run_id=payload.run_id,
            task_id=payload.task_id,
            blocked_reason=reason,
        )
        return reason
    if report is not None and report.status == "blocked":
        logger.info(
            "implementer reported blocked",
            run_id=payload.run_id,
            task_id=payload.task_id,
            blocked_reason=report.blocked_reason,
        )
        return report.blocked_reason or "agent reported blocked"
    return None


def resolve_target_repo(payload: ImplementerInput) -> str:
    """Pick the repo this run targets.

    Step Functions and the dashboard always thread ``target_repo``
    through the agent input, so the only failure mode is a malformed
    input.
    """
    if payload.target_repo:
        return payload.target_repo
    msg = "ImplementerInput.target_repo is required but missing"
    raise RuntimeError(msg)


def compose_prompt(
    payload: ImplementerInput, *, task_title: str, task_done_when: str | None
) -> str:
    """Compose the user message handed to Claude."""
    query = f"{task_title} — {task_done_when}" if task_done_when else task_title
    parts = [
        agent_memory_preamble(project_slug=payload.project_slug, query=query),
        f"Spec: {payload.spec_slug}  (files in /workspace/spec/)",
        f"Project: {payload.project_slug}  (repo at /workspace/repo/)",
        f"Task: {payload.task_id} — {task_title}",
    ]
    if task_done_when:
        parts.append(f"Done when: {task_done_when}")
    parts += [
        "",
        "Read /workspace/spec/requirements.md and /workspace/spec/design.md before "
        "you start. Make the smallest set of edits that satisfies this task's "
        "acceptance criteria. Run lint/format/type/test before you stop.",
    ]
    return "\n".join(parts)


def compose_iteration_prompt(
    payload: ImplementerInput,
    *,
    task_title: str,
    task_done_when: str | None,
    failed_checks: list[dict[str, Any]],
) -> str:
    """User message for an iteration run.

    The prior PR is already open; the agent's job here is to address the
    structured feedback (``payload.iteration_feedback``) — CI failures and
    @-mentioned PR comments — by pushing a fix commit on the existing
    branch and (optionally) including ``inline_replies`` in its
    ``finish`` report so threaded comments get acknowledged.
    """
    query = f"{task_title} — {task_done_when}" if task_done_when else task_title
    parts = [
        agent_memory_preamble(project_slug=payload.project_slug, query=query),
        (
            f"You are continuing **iteration {payload.iteration_count}** on the "
            f"existing PR for {payload.task_id}: {task_title}."
        ),
        (
            f"Project: {payload.project_slug}  (repo at /workspace/repo/, "
            "on branch already checked out)"
        ),
        f"Spec: {payload.spec_slug}  (files in /workspace/spec/)",
    ]
    if payload.pr_url:
        parts.append(f"PR: {payload.pr_url}")
    if task_done_when:
        parts.append(f"Done when: {task_done_when}")
    parts += ["", "Address the following feedback:"]
    for item in payload.iteration_feedback or []:
        parts.append(format_feedback_item(item))
    if failed_checks:
        parts += ["", "Failed CI check details:"]
        for check in failed_checks:
            parts.append(format_failed_check(check))
    parts += [
        "",
        "Push a fix commit on the existing branch — do NOT create a new branch or "
        "open a new PR. If any review comment warrants a written reply (clarification, "
        "agreement, follow-up question), include it in the `inline_replies` field of "
        "your finish report so the thread gets acknowledged. Run lint/format/type/test "
        "before you stop.",
    ]
    return "\n".join(parts)


def format_feedback_item(item: FeedbackItem) -> str:
    """Render one ``FeedbackItem`` into the iteration prompt as a bullet."""
    if isinstance(item, CiFailureFeedback):
        return (
            f"- **CI failure** in workflow `{item.workflow_name}` "
            f"(conclusion: `{item.conclusion}`, logs: {item.html_url})"
        )
    if isinstance(item, ReviewChangesRequestedFeedback):
        body = item.body.strip() or "(no review body)"
        return f"- **Review requested changes** by @{item.reviewer}: {body}"
    if isinstance(item, ReviewCommentMentionFeedback):
        loc = f"`{item.path}`" + (f":{item.line}" if item.line else "")
        return (
            f"- **Inline comment** at {loc} from @{item.commenter} "
            f"(comment_id={item.comment_id}): {item.body.strip()}"
        )
    if isinstance(item, IssueCommentMentionFeedback):
        return (
            f"- **PR comment** from @{item.commenter} "
            f"(comment_id={item.comment_id}): {item.body.strip()}"
        )
    msg = f"unknown feedback kind: {item!r}"
    raise TypeError(msg)


def format_failed_check(check: dict[str, Any]) -> str:
    """Render one failed check_run for the iteration prompt."""
    name = check.get("name", "?")
    conclusion = check.get("conclusion", "?")
    summary = (check.get("output") or {}).get("summary") or "(no summary)"
    details = check.get("html_url", "")
    return f"  - `{name}` ({conclusion}) — {summary} ({details})"


def any_ci_failure_feedback(feedback: list[FeedbackItem] | None) -> bool:
    """Quick check before paying for ``list_check_runs`` on the PR head."""
    if not feedback:
        return False
    return any(isinstance(item, CiFailureFeedback) for item in feedback)


def build_iteration_commit_message(task_id: str, title: str, iteration: int) -> str:
    """Imperative one-line commit subject for an iteration commit."""
    return f"{task_id}: iter {iteration} — {title}"


def build_blocked_commit_message(task_id: str, title: str) -> str:
    """Commit subject for a blocked first-pass — signals 'no implementation yet'."""
    return f"{task_id} (blocked): {title}"


def build_blocked_iteration_commit_message(task_id: str, title: str, iteration: int) -> str:
    """Commit subject for an iteration that remained blocked."""
    return f"{task_id} (blocked iter {iteration}): {title}"


async def run_lint_gate_with_retry(
    *,
    report: FinishReport | None,
    usage: dict[str, Any],
    run_id: str,
) -> tuple[LintGateResult, FinishReport | None, dict[str, Any]]:
    """Run the lint gate once; on failure feed errors back and retry once.

    Returns the final gate result, the (possibly updated) finish report,
    and any extra usage from the retry pass (zeros if no retry occurred).
    """
    gate = run_lint_gate(repo_path(), retry_count=0)
    extra_usage: dict[str, Any] = {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 0}

    if not gate.passed:
        feedback = compose_lint_feedback(gate)
        logger.info("lint gate failed on first pass; retrying", run_id=run_id)
        retry_report, retry_usage = await drive_agent(feedback, run_id=run_id)
        extra_usage = retry_usage
        if retry_report is not None:
            report = retry_report
        gate = run_lint_gate(repo_path(), retry_count=1)
        if gate.passed:
            logger.info("lint gate passed after retry", run_id=run_id)
        else:
            logger.warning("lint gate failed after retry; proceeding to commit", run_id=run_id)

    return gate, report, extra_usage


def merge_usage(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Accumulate usage totals from a retry pass into the base totals."""
    return {
        "token_in": base["token_in"] + extra["token_in"],
        "token_out": base["token_out"] + extra["token_out"],
        "cost_usd": base["cost_usd"] + extra["cost_usd"],
        "duration_ms": base["duration_ms"] + extra["duration_ms"],
    }


async def drive_agent(
    user_prompt: str,
    *,
    run_id: str,
) -> tuple[FinishReport | None, dict[str, Any]]:
    """Run one ClaudeSDKClient session.

    Returns a tuple of:

    * the agent's structured ``finish`` report, or ``None`` if the agent
      ended without calling ``finish``;
    * a dict of usage fields (``token_in``, ``token_out``, ``cost_usd``,
      ``duration_ms``) sourced from the SDK's :class:`ResultMessage`.

    The Claude Agent SDK reports cost directly, so no pricing-table
    lookup is needed for the implementer.
    """
    sink = FinishSink()
    options = build_options(run_id, finish_sink=sink)
    usage: dict[str, Any] = {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 0}
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                usage = extract_usage(msg)
                logger.info(
                    "session done",
                    session_id=msg.session_id,
                    cost_usd=usage["cost_usd"],
                    token_in=usage["token_in"],
                    token_out=usage["token_out"],
                    duration_ms=usage["duration_ms"],
                )
    return sink.report, usage


def extract_usage(msg: ResultMessage) -> dict[str, Any]:
    """Pull the four usage fields off a Claude Agent SDK ResultMessage."""
    raw = msg.usage or {}
    return {
        "token_in": int(raw.get("input_tokens", 0) or 0),
        "token_out": int(raw.get("output_tokens", 0) or 0),
        "cost_usd": float(msg.total_cost_usd or 0.0),
        "duration_ms": int(msg.duration_ms or 0),
    }


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


def blocked_md_path(spec_slug: str) -> str:
    """Repo-relative path to the per-spec BLOCKED.md."""
    return f"docs/specs/{spec_slug}/BLOCKED.md"


def write_blocked_md(
    *,
    spec_slug: str,
    task_id: str,
    blocked_reason: str,
    report: FinishReport | None,
) -> None:
    """Materialise ``BLOCKED.md`` so the draft PR has a meaningful diff.

    The file lives at ``docs/specs/{spec_slug}/BLOCKED.md`` so it sits
    alongside the spec docs and is naturally cleaned up when the
    iteration produces a real diff (see :func:`delete_blocked_md`).
    The body restates the task, the agent's blocker, and the agent's
    self-report so a human can read it directly in the GitHub PR view.
    """
    target = repo_path() / "docs" / "specs" / spec_slug / "BLOCKED.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Implementation blocked: {task_id}",
        "",
        f"> **spec_slug:** `{spec_slug}` · **task:** `{task_id}`",
        "",
        "## Blocker",
        "",
        blocked_reason.strip(),
        "",
        "## How to advance",
        "",
        "- **Continue**: comment on this PR with `@aidlc-bot <guidance>` "
        "to retry the implementation with that guidance as feedback.",
        "- **Abort this task**: close this PR. Other tasks in the run (if any) keep running.",
        "",
    ]
    if report is not None:
        if report.summary:
            lines += ["## Agent summary", "", report.summary.strip(), ""]
        if report.risks:
            lines += ["## Risks the agent flagged", ""]
            lines += [f"- {risk}" for risk in report.risks]
            lines += [""]
    target.write_text("\n".join(lines), encoding="utf-8")


def delete_blocked_md(spec_slug: str) -> None:
    """Remove ``BLOCKED.md`` from the working tree if present.

    Called on iteration paths that produce a real diff so the
    blocked-state artifact doesn't ride along into ``main`` when the
    PR is merged.
    """
    target = repo_path() / "docs" / "specs" / spec_slug / "BLOCKED.md"
    if target.exists():
        target.unlink()


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

    Sections (in order, omitted when empty): Summary, Tests, Risks. The
    list of changed files is intentionally not duplicated — GitHub's PR
    view already shows the diff, and the agent's self-reported list
    misses platform-added files (spec materialization, ``tasks.md``
    flip), so emitting it would mislead reviewers about scope. Always
    emits a footer with run + correlation IDs and a link to the in-repo
    spec folder. If ``report`` is ``None``, emits a fallback body
    explaining that ``finish`` was not called.
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
        pr_body_footer(payload),
    ]
    return "\n".join(lines)


def render_no_finish_body(payload: ImplementerInput, *, task_title: str) -> str:
    """Fallback PR body when the agent never called ``finish``."""
    return (
        f"## {payload.task_id}: {task_title}\n\n"
        "_Implementer ended the session without calling the `finish` tool — "
        "no structured summary is available. See the diff for details._\n\n"
        f"---\n{pr_body_footer(payload)}"
    )


def render_blocked_pr_body(
    payload: ImplementerInput,
    *,
    task_title: str,
    blocked_reason: str,
    report: FinishReport | None,
) -> str:
    """Render the PR body for a blocked task.

    The blocked PR is the system's request for human guidance. The body
    leads with the blocker and the two ways to advance — comment to
    continue, close to abort the task — so a reviewer can act without
    reading the whole conversation. The diff itself is just
    ``BLOCKED.md`` (which carries the same info in-repo).
    """
    lines = [
        f"## {payload.task_id} (blocked): {task_title}",
        "",
        "**The implementer could not produce changes for this task and is asking for guidance.**",
        "",
        "### Blocker",
        "",
        blocked_reason.strip(),
        "",
        "### How to advance this PR",
        "",
        "- **Continue**: comment with `@aidlc-bot <your guidance>` and "
        "the implementer will retry with your comment as feedback.",
        "- **Abort this task**: close this PR. Other tasks in the run (if any) keep running.",
        "",
    ]
    if report is not None:
        if report.summary:
            lines += ["### Agent summary", "", report.summary.strip(), ""]
        if report.risks:
            lines += ["### Risks the agent flagged", ""]
            lines += [f"- {risk}" for risk in report.risks]
            lines += [""]
    lines += ["---", pr_body_footer(payload)]
    return "\n".join(lines)


def pr_body_footer(payload: ImplementerInput) -> str:
    """Human-readable footer with provenance refs + run context.

    Two lines: a ``Refs:`` line carrying the originating GitHub issue,
    the merged spec PR, and the in-repo spec path; then the italicised
    run/correlation/project/task identifiers.

    The issue and spec PR URLs are populated by the state router on
    issue-driven runs; programmatic runs (POST ``/v1/runs`` without a
    source issue) pass ``None`` and the line gracefully omits the
    missing entries.

    The dashboard webhook resolves the run/task by querying the runs
    table's ``gsi_pr`` index on ``pr_url`` — no PR-body parsing —
    so this footer is informational only.
    """
    refs = []
    if payload.source_issue_url:
        refs.append(f"issue: {payload.source_issue_url}")
    if payload.spec_pr_url:
        refs.append(f"spec PR: {payload.spec_pr_url}")
    refs.append(f"spec: `docs/specs/{payload.spec_slug}/`")
    return (
        f"Refs: {' · '.join(refs)}\n\n"
        f"_run_id: {payload.run_id}_  ·  "
        f"_correlation_id: {payload.correlation_id}_  ·  "
        f"_project: {payload.project_slug}_  ·  "
        f"_task: {payload.task_id}_"
    )
