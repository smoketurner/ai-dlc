"""Drives one Claude Agent SDK invocation for a single task.

Flow per invocation:

  1. Clone main, fetch the run's impl branch, check it out as the base.
  2. Download the spec bundle from S3 into ``/workspace/spec``.
  3. Create a task branch off the impl branch tip.
  4. Run Claude on the task. The agent edits, commits via the wrapper.
  5. Push the task branch.
  6. Call GitHub's server-side merge API to merge the task branch into
     the impl branch. On conflict, run a constrained Claude sub-session
     to reconcile the merge; retry the merge. After
     ``MAX_CONFLICT_RESOLVE_ATTEMPTS`` failures, write ``BLOCKED.md``
     on the task branch and surface ``TASK.BLOCKED``.

The unified impl PR is opened by the state router on the first task
event for the run — not by this code.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
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
from implementer.options import build_options, build_resolver_options
from implementer.prompts import RESOLVER_USER_TEMPLATE
from implementer.repo_ops import (
    abort_merge,
    agent_made_real_changes,
    checkout_impl_branch,
    checkout_task_branch,
    clone_repo,
    commit_changes,
    create_branch,
    fetch_branch,
    fetch_failed_check_runs,
    fetch_spec,
    has_uncommitted_changes,
    impl_branch_name,
    invoke_repo_helper,
    make_session,
    parse_pr_number,
    post_inline_replies,
    push_branch,
    repo_path,
    run_git,
    short_diff_summary,
    spec_path,
    task_branch_name,
    unmerged_paths,
)
from implementer.tasks import find_task, parse_tasks

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = structlog.get_logger()

MAX_CONFLICT_RESOLVE_ATTEMPTS = 2
"""How many times the resolver agent runs before we declare BLOCKED."""


@dataclass(frozen=True)
class MergeOutcome:
    """Result of :func:`merge_with_resolution`.

    ``error`` is non-empty when the loop exhausted retries or hit a
    non-conflict failure; the wrapper writes ``BLOCKED.md`` and emits
    ``TASK.BLOCKED``. ``resolutions`` counts how many times the
    resolver agent successfully reconciled a conflict.
    """

    success: bool
    error: str = ""
    resolutions: int = 0


async def execute_task(payload: ImplementerInput) -> ImplementerResult:
    """Dispatch to the iteration vs initial flow based on ``iteration_count``."""
    if payload.iteration_count > 0:
        return await execute_iteration(payload)
    return await execute_initial(payload)


async def execute_initial(payload: ImplementerInput) -> ImplementerResult:
    """First-pass flow: branch off impl, agent edits, merge into impl branch."""
    target_repo = resolve_target_repo(payload)
    session = make_session(target_repo=target_repo, requestor_sub=payload.requestor_sub)
    impl_branch = impl_branch_name(payload.spec_slug, payload.run_id)
    task_branch = task_branch_name(payload.task_id, payload.spec_slug, payload.run_id)
    logger.info(
        "implementer session opened",
        run_id=payload.run_id,
        target_repo=session.target_repo,
        impl_branch=impl_branch,
        task_branch=task_branch,
        on_behalf_of_user=session.on_behalf_of_user,
    )

    clone_repo(session)
    checkout_impl_branch(impl_branch)
    fetch_spec(payload.spec_s3_prefix)

    task = load_task(payload)
    create_branch(task_branch)

    user_prompt = compose_prompt(payload, task_title=task.title, task_done_when=task.done_when)
    report, usage = await drive_agent(user_prompt, run_id=payload.run_id)

    blocked_reason = compute_blocked_reason(payload, report, base=impl_branch)
    if blocked_reason is None and run_cancelled(payload.run_id):
        blocked_reason = "run cancelled"

    finalize_task_branch(
        spec_slug=payload.spec_slug,
        task_id=payload.task_id,
        task_title=task.title,
        task_branch=task_branch,
        blocked_reason=blocked_reason,
        report=report,
    )

    if blocked_reason is None:
        outcome = await merge_with_resolution(
            session=session,
            impl_branch=impl_branch,
            task_branch=task_branch,
            run_id=payload.run_id,
            requestor_sub=payload.requestor_sub,
        )
        if not outcome.success:
            blocked_reason = outcome.error
            record_merge_blocker(
                spec_slug=payload.spec_slug,
                task_id=payload.task_id,
                task_title=task.title,
                task_branch=task_branch,
                blocked_reason=blocked_reason,
                report=report,
                iteration=0,
            )

    return ImplementerResult(
        task_id=payload.task_id,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
        blocked_reason=blocked_reason,
        **usage,
    )


async def execute_iteration(payload: ImplementerInput) -> ImplementerResult:
    """Iteration flow: pull latest impl branch, agent fixes, merge in.

    Iterations always re-merge the impl branch into the task branch
    before the agent runs — sibling task commits may have landed
    since the prior iteration. If that pre-flight merge conflicts,
    the resolver agent reconciles; otherwise the agent runs against a
    current view of the run's shared state.
    """
    target_repo = resolve_target_repo(payload)
    session = make_session(target_repo=target_repo, requestor_sub=payload.requestor_sub)
    impl_branch = impl_branch_name(payload.spec_slug, payload.run_id)
    task_branch = task_branch_name(payload.task_id, payload.spec_slug, payload.run_id)
    logger.info(
        "implementer iteration session opened",
        run_id=payload.run_id,
        iteration=payload.iteration_count,
        target_repo=session.target_repo,
        impl_branch=impl_branch,
        task_branch=task_branch,
    )

    clone_repo(session)
    checkout_task_branch(task_branch, impl_branch_fallback=impl_branch)
    fetch_spec(payload.spec_s3_prefix)
    task = load_task(payload)

    pre_flight_error = await pre_flight_merge_impl_branch(
        impl_branch=impl_branch,
        run_id=payload.run_id,
        attempt=payload.iteration_count,
    )
    if pre_flight_error is not None:
        return iteration_blocked(
            payload,
            task_title=task.title,
            error=pre_flight_error,
            task_branch=task_branch,
        )

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

    blocked_reason = compute_blocked_reason(payload, report, base=impl_branch)
    if blocked_reason is None and run_cancelled(payload.run_id):
        blocked_reason = "run cancelled"

    finalize_iteration_branch(
        spec_slug=payload.spec_slug,
        task_id=payload.task_id,
        task_title=task.title,
        task_branch=task_branch,
        iteration=payload.iteration_count,
        blocked_reason=blocked_reason,
        report=report,
    )

    if blocked_reason is None:
        outcome = await merge_with_resolution(
            session=session,
            impl_branch=impl_branch,
            task_branch=task_branch,
            run_id=payload.run_id,
            requestor_sub=payload.requestor_sub,
        )
        if not outcome.success:
            blocked_reason = outcome.error
            record_merge_blocker(
                spec_slug=payload.spec_slug,
                task_id=payload.task_id,
                task_title=task.title,
                task_branch=task_branch,
                blocked_reason=blocked_reason,
                report=report,
                iteration=payload.iteration_count,
            )

    if report is not None and report.inline_replies and payload.pr_url is not None:
        post_inline_replies(
            repo=target_repo,
            pr_number=parse_pr_number(payload.pr_url),
            requestor_sub=payload.requestor_sub,
            replies=[(r.comment_id, r.body) for r in report.inline_replies],
        )

    return ImplementerResult(
        task_id=payload.task_id,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
        blocked_reason=blocked_reason,
        **usage,
    )


def load_task(payload: ImplementerInput) -> Any:
    """Read tasks.md from the workspace and return the matching task."""
    tasks_md = (spec_path() / "tasks.md").read_text(encoding="utf-8")
    task = find_task(parse_tasks(tasks_md), payload.task_id)
    if task is None:
        msg = f"task_id={payload.task_id!r} not found in {payload.spec_s3_prefix}tasks.md"
        raise KeyError(msg)
    return task


def finalize_task_branch(
    *,
    spec_slug: str,
    task_id: str,
    task_title: str,
    task_branch: str,
    blocked_reason: str | None,
    report: FinishReport | None,
) -> None:
    """Commit + push the task branch in either the happy or blocked path."""
    if blocked_reason is not None:
        write_blocked_md(
            spec_slug=spec_slug,
            task_id=task_id,
            blocked_reason=blocked_reason,
            report=report,
        )
        commit_message = build_blocked_commit_message(task_id, task_title)
    else:
        commit_message = build_commit_message(task_id, task_title)
    if has_uncommitted_changes():
        commit_changes(commit_message)
    push_branch(task_branch)


def finalize_iteration_branch(
    *,
    spec_slug: str,
    task_id: str,
    task_title: str,
    task_branch: str,
    iteration: int,
    blocked_reason: str | None,
    report: FinishReport | None,
) -> None:
    """Iteration counterpart of :func:`finalize_task_branch`."""
    if blocked_reason is not None:
        write_blocked_md(
            spec_slug=spec_slug,
            task_id=task_id,
            blocked_reason=blocked_reason,
            report=report,
        )
        commit_message = build_blocked_iteration_commit_message(task_id, task_title, iteration)
    else:
        delete_blocked_md(spec_slug)
        commit_message = build_iteration_commit_message(task_id, task_title, iteration)
    if has_uncommitted_changes():
        commit_changes(commit_message)
    push_branch(task_branch)


def record_merge_blocker(
    *,
    spec_slug: str,
    task_id: str,
    task_title: str,
    task_branch: str,
    blocked_reason: str,
    report: FinishReport | None,
    iteration: int,
) -> None:
    """Rewrite ``BLOCKED.md`` after a merge failure and push the update."""
    if unmerged_paths():
        abort_merge()
    write_blocked_md(
        spec_slug=spec_slug,
        task_id=task_id,
        blocked_reason=blocked_reason,
        report=report,
    )
    if iteration == 0:
        commit_message = build_blocked_commit_message(task_id, task_title)
    else:
        commit_message = build_blocked_iteration_commit_message(task_id, task_title, iteration)
    if has_uncommitted_changes():
        commit_changes(commit_message)
        push_branch(task_branch)


def iteration_blocked(
    payload: ImplementerInput,
    *,
    task_title: str,
    error: str,
    task_branch: str,
) -> ImplementerResult:
    """Build a blocked result without running the agent (pre-flight merge failed)."""
    write_blocked_md(
        spec_slug=payload.spec_slug,
        task_id=payload.task_id,
        blocked_reason=error,
        report=None,
    )
    if has_uncommitted_changes():
        commit_changes(
            build_blocked_iteration_commit_message(
                payload.task_id,
                task_title,
                payload.iteration_count,
            ),
        )
        push_branch(task_branch)
    return ImplementerResult(
        task_id=payload.task_id,
        diff_summary="",
        session_id=payload.run_id,
        blocked_reason=error,
        token_in=0,
        token_out=0,
        cost_usd=0.0,
        duration_ms=0,
    )


async def pre_flight_merge_impl_branch(
    *,
    impl_branch: str,
    run_id: str,
    attempt: int,
) -> str | None:
    """Merge ``origin/{impl_branch}`` into the iteration's task branch.

    Returns ``None`` when the merge completed cleanly (auto-merge or
    resolver-reconciled). Returns an error string when the resolver
    exhausted its attempts — caller surfaces it as ``TASK.BLOCKED``
    without running the main agent.
    """
    fetch_branch(impl_branch)
    with contextlib.suppress(RuntimeError):
        run_git("merge", f"origin/{impl_branch}", "--no-edit")
    conflicted = unmerged_paths()
    if not conflicted:
        return None
    resolved = await resolve_conflict_with_agent(
        impl_branch=impl_branch,
        attempt=attempt,
        run_id=run_id,
    )
    if not resolved:
        abort_merge()
        return f"pre-flight merge of {impl_branch} could not be reconciled"
    return None


async def merge_with_resolution(
    *,
    session: Any,
    impl_branch: str,
    task_branch: str,
    run_id: str,
    requestor_sub: str | None,
) -> MergeOutcome:
    """Merge ``task_branch`` into ``impl_branch`` via the GitHub merges API.

    On 409 conflict, run the resolver agent to reconcile, push the
    resolution commit, and retry — up to
    :data:`MAX_CONFLICT_RESOLVE_ATTEMPTS` times. On non-conflict
    failure (404 base/head missing, 5xx, etc.) return immediately.
    """
    resolutions = 0
    for attempt in range(MAX_CONFLICT_RESOLVE_ATTEMPTS + 1):
        result = invoke_repo_helper(
            op="merge_branch",
            requestor_sub=requestor_sub,
            repo=session.target_repo,
            base=impl_branch,
            head=task_branch,
            commit_message=f"merge {task_branch} into {impl_branch}",
            delete_head_on_merge=True,
        )
        if result.get("merged"):
            return MergeOutcome(success=True, resolutions=resolutions)
        if result.get("not_found"):
            return MergeOutcome(success=False, error=f"merge_branch not_found: {result}")
        if not result.get("conflict"):
            return MergeOutcome(success=False, error=f"merge_branch failed: {result}")
        if attempt >= MAX_CONFLICT_RESOLVE_ATTEMPTS:
            return MergeOutcome(
                success=False,
                error=(
                    f"merge conflict on {impl_branch} after "
                    f"{MAX_CONFLICT_RESOLVE_ATTEMPTS} resolver attempts"
                ),
                resolutions=resolutions,
            )
        resolved = await resolve_conflict_with_agent(
            impl_branch=impl_branch,
            attempt=attempt + 1,
            run_id=run_id,
        )
        if not resolved:
            abort_merge()
            return MergeOutcome(
                success=False,
                error="conflict resolver could not reconcile",
                resolutions=resolutions,
            )
        push_branch(task_branch)
        resolutions += 1
    return MergeOutcome(success=False, error="merge loop exhausted", resolutions=resolutions)


async def resolve_conflict_with_agent(
    *,
    impl_branch: str,
    attempt: int,
    run_id: str,
) -> bool:
    """Run a constrained Claude sub-session that resolves conflict markers.

    Returns ``True`` when every conflict marker is gone after the
    session ends; the caller commits the resolution. Returns ``False``
    when conflicts remain — the caller aborts the merge and surfaces
    ``TASK.BLOCKED``.
    """
    fetch_branch(impl_branch)
    with contextlib.suppress(RuntimeError):
        run_git("merge", f"origin/{impl_branch}", "--no-edit")
    conflicted = unmerged_paths()
    if not conflicted:
        if has_uncommitted_changes():
            commit_changes(f"merge {impl_branch} (auto)")
        logger.info("resolver: clean auto-merge, no agent needed", run_id=run_id, attempt=attempt)
        return True

    head_sha = run_git("rev-parse", f"origin/{impl_branch}").strip()
    user_prompt = RESOLVER_USER_TEMPLATE.format(
        impl_branch=impl_branch,
        impl_sha=head_sha,
        conflicted_files="\n".join(f"- {p}" for p in conflicted),
    )
    options = build_resolver_options()
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                logger.info(
                    "resolver session done",
                    run_id=run_id,
                    attempt=attempt,
                    cost_usd=msg.total_cost_usd,
                    duration_ms=msg.duration_ms,
                )

    remaining = unmerged_paths()
    if remaining:
        logger.warning(
            "resolver left unresolved markers",
            run_id=run_id,
            attempt=attempt,
            files=remaining,
        )
        return False
    run_git("add", "-A")
    run_git("commit", "-m", f"resolve merge conflicts with {impl_branch} (attempt {attempt})")
    return True


# ---------------------------------------------------------------------------
# Cancellation check — read STATE row, return True if cancelled/failed
# ---------------------------------------------------------------------------


@cache
def ddb_client() -> DynamoDBClient:
    """Process-cached DDB client (cancellation check)."""
    return boto3.client("dynamodb")


def runs_table_name() -> str | None:
    """Runs table name, ``None`` when not wired (local dev)."""
    return os.environ.get("AIDLC_RUNS_TABLE") or None


def run_cancelled(run_id: str) -> bool:
    """``True`` when the run's STATE row is in ``cancelled`` or ``failed``.

    Skips the check when ``AIDLC_RUNS_TABLE`` is unset (local dev /
    tests). A read failure is treated as not-cancelled — fail-open is
    safer here than blocking the merge on a transient DDB error.
    """
    table = runs_table_name()
    if table is None:
        return False
    try:
        response = ddb_client().get_item(
            TableName=table,
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
            ProjectionExpression="current_state",
        )
    except Exception as exc:
        logger.warning("run_cancelled lookup failed", run_id=run_id, error=str(exc))
        return False
    state = (response.get("Item") or {}).get("current_state", {}).get("S", "")
    return state in {"cancelled", "failed"}


# ---------------------------------------------------------------------------
# Result-classification helpers (unchanged from prior client.py)
# ---------------------------------------------------------------------------


def compute_blocked_reason(
    payload: ImplementerInput,
    report: FinishReport | None,
    *,
    base: str,
) -> str | None:
    """Return the agent's blocker explanation, or ``None`` on a real diff.

    A blocked path lands when the agent makes no real changes (``status
    --porcelain`` is empty and ``HEAD vs origin/{base}`` has no diff
    outside the spec tree) or the agent itself reported
    ``status='blocked'`` via the ``finish`` tool.
    """
    if report is None:
        logger.warning(
            "implementer ended without calling finish",
            run_id=payload.run_id,
            task_id=payload.task_id,
        )
    if not agent_made_real_changes(payload.spec_slug, base=base):
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
    """Pick the repo this run targets. Required field on ImplementerInput."""
    if payload.target_repo:
        return payload.target_repo
    msg = "ImplementerInput.target_repo is required but missing"
    raise RuntimeError(msg)


def compose_prompt(
    payload: ImplementerInput, *, task_title: str, task_done_when: str | None
) -> str:
    """Compose the user message handed to Claude for the first-pass agent run."""
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
    """Compose the user message for an iteration agent run."""
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


async def drive_agent(
    user_prompt: str,
    *,
    run_id: str,
) -> tuple[FinishReport | None, dict[str, Any]]:
    """Run one ClaudeSDKClient session for the main task agent."""
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


# ---------------------------------------------------------------------------
# Commit-message + BLOCKED.md helpers
# ---------------------------------------------------------------------------


def build_commit_message(task_id: str, title: str) -> str:
    """Imperative one-line commit subject."""
    return f"{task_id}: {title}"


def build_iteration_commit_message(task_id: str, title: str, iteration: int) -> str:
    """Commit subject for an iteration commit."""
    return f"{task_id}: iter {iteration} — {title}"


def build_blocked_commit_message(task_id: str, title: str) -> str:
    """Commit subject for a blocked first-pass."""
    return f"{task_id} (blocked): {title}"


def build_blocked_iteration_commit_message(task_id: str, title: str, iteration: int) -> str:
    """Commit subject for a blocked iteration."""
    return f"{task_id} (blocked iter {iteration}): {title}"


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
    """Materialise ``BLOCKED.md`` on the task branch so a human can intervene."""
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
        "- **Continue**: comment `@aidlc-bot <guidance>` on the impl PR to "
        "retry with that guidance as feedback.",
        "- **Abort this task**: close the impl PR. Other tasks in the run keep running.",
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
    """Remove ``BLOCKED.md`` from the working tree if present."""
    target = repo_path() / "docs" / "specs" / spec_slug / "BLOCKED.md"
    if target.exists():
        target.unlink()
