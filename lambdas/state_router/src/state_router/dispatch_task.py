"""Task-state dispatch — pure functions ``(Run, Task) -> Action``.

Each task carries its own state machine; this module decides what
to do next for one task given its current state. Run-level dispatch
walks ``run.tasks`` and calls :func:`decide_task` per task.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from common.events import EventEnvelope, TaskBlocked
from common.ids import CorrelationId, RunId, new_event_id
from common.state import TaskState
from state_router.actions import (
    Action,
    DedupedAdvisors,
    EmitEvent,
    GuardedAdvance,
    InvokeAgent,
    Noop,
)
from state_router.config import runtime_arn

if TYPE_CHECKING:
    from state_router.model import Run, Task

type TaskHandler = Callable[["Run", "Task"], Action]


DEPENDS_ON_SATISFIED_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.pr_open,
        TaskState.reviewer_running,
        TaskState.tester_running,
        TaskState.pending_approval,
        TaskState.iterating,
        TaskState.blocked,
        TaskState.merged,
    },
)
"""Predecessor task states that count as satisfying a ``depends_on``.

A predecessor must have merged into the impl branch before its
dependent runs — ``pr_open`` and later are the proof of that merge.
``closed`` / ``failed`` are terminal-not-merged: a dependent on a
failed predecessor stays blocked because the merge never happened.
"""


def decide_task(run: Run, task: Task) -> Action:
    """Dispatch one task based on its current state.

    Pure: takes both the run (for context like spec_slug, target_repo)
    and the task (for state-specific data like pr_url, iteration_count).

    For ``pending`` tasks, ``depends_on`` is enforced first: a missing
    predecessor (validation bypassed) is a permanent block; an
    in-progress predecessor is a wait-then-retry.
    """
    if task.state == TaskState.pending and task.depends_on:
        action = check_depends_on(run, task)
        if action is not None:
            return action
    handler = TASK_DISPATCH.get(task.state)
    if handler is None:
        return Noop(f"unknown task state: {task.state}")
    return handler(run, task)


def check_depends_on(run: Run, task: Task) -> Action | None:
    """Walk ``task.depends_on``; return a Noop / EmitEvent if any blocks dispatch.

    Returns ``None`` when every predecessor has reached a satisfied
    state — caller proceeds to the normal pending dispatch.
    """
    tasks_by_id = {t.task_id: t for t in run.tasks}
    waiting: list[str] = []
    for predecessor_id in task.depends_on:
        predecessor = tasks_by_id.get(predecessor_id)
        if predecessor is None:
            return emit_task_blocked(
                run,
                task,
                reason=f"depends_on references unknown task {predecessor_id!r}",
            )
        if predecessor.state not in DEPENDS_ON_SATISFIED_STATES:
            waiting.append(predecessor_id)
    if waiting:
        return Noop(f"task {task.task_id} waiting for depends_on: {', '.join(waiting)}")
    return None


def emit_task_blocked(run: Run, task: Task, *, reason: str) -> EmitEvent:
    """Emit ``TASK.BLOCKED`` so the projector advances this task to ``blocked``."""
    return EmitEvent(
        envelope=EventEnvelope[TaskBlocked](
            event_id=new_event_id(),
            type="TASK.BLOCKED",
            run_id=RunId(run.run_id),
            correlation_id=CorrelationId(run.correlation_id),
            actor_id="state_router",
            payload=TaskBlocked(
                project_slug=run.project_slug,
                spec_slug=run.spec_slug or "",
                task_id=task.task_id,
                blocked_reason=reason,
                session_id=run.run_id,
            ),
        ),
    )


def dispatch_implementer(run: Run, task: Task) -> Action:
    """Pending task — fire the implementer for the first iteration."""
    arn = runtime_arn("implementer")
    if not arn:
        return Noop("implementer runtime ARN not yet provisioned")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-{task.task_id}",
        payload={
            "project_slug": run.project_slug,
            "spec_slug": run.spec_slug,
            "spec_s3_prefix": run.spec_s3_prefix,
            "task_id": task.task_id,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": "state_router",
            "iteration_count": 0,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
            "source_issue_url": run.source_issue_url,
            "spec_pr_url": run.pr_url,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.pending.value,
        advance_to=TaskState.implementer_running.value,
    )


def dispatch_advisors(run: Run, task: Task) -> Action:
    """PR is open — fire reviewer + tester in parallel, dedup'd by PR head SHA.

    The :class:`GuardedAdvance` flips the task ``pr_open → pending_approval``;
    only the winning router runs ``on_success``. The inner
    :class:`DedupedAdvisors` action then fetches the PR head SHA and
    skips the advisor invocations if it matches ``run.last_advisor_sha``
    — sibling-task merges that move the SHA between this beacon and
    a redelivery would otherwise re-fire reviewer + tester on the same
    diff.

    Reviewer's ``REVIEW.READY`` and tester's ``TEST_REPORT.READY``
    events are advisory and do not change task state; advisors post
    their findings as PR comments while we wait for human merge or
    review verdict. When the task hasn't yet been linked to a PR
    (``task.pr_url`` empty — first task in the run, impl PR opening
    in flight), Noop and let the next beacon retry once pr_url is set.
    """
    if not task.pr_url or not run.target_repo:
        return Noop(f"task {task.task_id} pr_url not yet set; waiting for impl PR open")
    invokes: list[InvokeAgent] = []
    reviewer_arn = runtime_arn("reviewer")
    tester_arn = runtime_arn("tester")
    if reviewer_arn:
        invokes.append(invoke_reviewer(run, task, reviewer_arn))
    if tester_arn:
        invokes.append(invoke_tester(run, task, tester_arn))
    return GuardedAdvance(
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.pr_open.value,
        advance_to=TaskState.pending_approval.value,
        on_success=(
            DedupedAdvisors(
                repo=run.target_repo,
                pr_url=task.pr_url,
                advisors=tuple(invokes),
            ),
        ),
    )


def invoke_reviewer(run: Run, task: Task, arn: str) -> InvokeAgent:
    """Fire the reviewer against the task's PR. Gated by the outer GuardedAdvance."""
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-{task.task_id}-reviewer",
        payload={
            "project_slug": run.project_slug,
            "spec_slug": run.spec_slug,
            "spec_s3_prefix": run.spec_s3_prefix,
            "task_id": task.task_id,
            "pr_url": task.pr_url,
            "diff_summary": "",
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": "state_router",
            "requestor_sub": run.requestor_sub,
        },
    )


def invoke_tester(run: Run, task: Task, arn: str) -> InvokeAgent:
    """Fire the tester against the task's PR. Gated by the outer GuardedAdvance."""
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-{task.task_id}-tester",
        payload={
            "project_slug": run.project_slug,
            "spec_slug": run.spec_slug,
            "spec_s3_prefix": run.spec_s3_prefix,
            "task_id": task.task_id,
            "pr_url": task.pr_url,
            "diff_summary": "",
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": "state_router",
            "requestor_sub": run.requestor_sub,
        },
    )


def dispatch_iteration(run: Run, task: Task) -> Action:
    """Iteration was requested — fire the implementer with pending feedback."""
    arn = runtime_arn("implementer")
    if not arn:
        return Noop("implementer runtime ARN not yet provisioned")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-{task.task_id}",
        payload={
            "project_slug": run.project_slug,
            "spec_slug": run.spec_slug,
            "spec_s3_prefix": run.spec_s3_prefix,
            "task_id": task.task_id,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": "state_router",
            "iteration_count": task.iteration_count + 1,
            "iteration_feedback": list(task.pending_feedback),
            "pr_url": task.pr_url,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
            "source_issue_url": run.source_issue_url,
            "spec_pr_url": run.pr_url,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.iterating.value,
        advance_to=TaskState.implementer_running.value,
    )


def task_noop_waiting(_run: Run, task: Task) -> Action:
    """No-op for task states waiting on external events."""
    return Noop(f"task {task.task_id} waiting in {task.state}")


def task_terminal(_run: Run, task: Task) -> Action:
    """Terminal task state — handled by the parent run's tasks_complete check."""
    return Noop(f"task {task.task_id} terminal: {task.state}")


TASK_DISPATCH: Mapping[TaskState, TaskHandler] = {
    TaskState.pending: dispatch_implementer,
    TaskState.implementer_running: task_noop_waiting,
    TaskState.pr_open: dispatch_advisors,
    TaskState.reviewer_running: task_noop_waiting,
    TaskState.tester_running: task_noop_waiting,
    TaskState.iterating: dispatch_iteration,
    TaskState.pending_approval: task_noop_waiting,
    # ``blocked`` waits for a human to comment on the draft PR (which fires
    # TASK.ITERATION_REQUESTED via the existing webhook path) or close it
    # (TASK.REJECTED). Reviewer + Tester intentionally do NOT fire — there's
    # no implementation in the PR to review.
    TaskState.blocked: task_noop_waiting,
    TaskState.merged: task_terminal,
    TaskState.closed: task_terminal,
    TaskState.failed: task_terminal,
}
