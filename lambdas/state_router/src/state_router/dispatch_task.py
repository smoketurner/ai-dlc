"""Task-state dispatch — pure functions ``(Run, Task) -> Action``.

Each task carries its own state machine; this module decides what
to do next for one task given its current state. Run-level dispatch
walks ``run.tasks`` and calls :func:`decide_task` per task.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from common.state import TaskState
from state_router.actions import (
    Action,
    GuardedAdvance,
    InvokeAgent,
    Noop,
)
from state_router.config import runtime_arn

if TYPE_CHECKING:
    from state_router.model import Run, Task

type TaskHandler = Callable[["Run", "Task"], Action]


def decide_task(run: Run, task: Task) -> Action:
    """Dispatch one task based on its current state.

    Pure: takes both the run (for context like spec_slug, target_repo)
    and the task (for state-specific data like pr_url, iteration_count).
    """
    handler = TASK_DISPATCH.get(task.state)
    if handler is None:
        return Noop(f"unknown task state: {task.state}")
    return handler(run, task)


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
    """PR is open — fire reviewer + tester in parallel, race-protected.

    The :class:`GuardedAdvance` flips the task ``pr_open → pending_approval``;
    only the winning router runs ``on_success`` and fires the advisors.
    A loser (e.g., a redelivered beacon while the original consumer was
    still mid-execution) sees the new state on its next read and no-ops.
    Without the gate, ``advance_from == advance_to`` is a no-op
    conditional that always succeeds — every concurrent router would
    fire both advisors, doubling cost and PR comment noise.

    Reviewer's ``REVIEW.READY`` and tester's ``TEST_REPORT.READY``
    events are advisory and do not change task state; advisors post
    their findings as PR comments while we wait for human merge or PR
    review verdict.
    """
    fires: list[Action] = []
    reviewer_arn = runtime_arn("reviewer")
    tester_arn = runtime_arn("tester")
    if reviewer_arn:
        fires.append(invoke_reviewer(run, task, reviewer_arn))
    if tester_arn:
        fires.append(invoke_tester(run, task, tester_arn))
    return GuardedAdvance(
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.pr_open.value,
        advance_to=TaskState.pending_approval.value,
        on_success=tuple(fires),
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
