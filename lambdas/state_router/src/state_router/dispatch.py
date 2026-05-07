"""Dispatch tables + handlers for the state router.

Each handler is a **pure function** from a :class:`~.model.Run` (or
``(Run, Task)`` for task-level dispatch) to an :data:`~.actions.Action`.
The router :mod:`.handler` walks the resulting action and executes its
side effects.

Adding a new state to the platform = adding one entry to
:data:`RUN_DISPATCH` (or :data:`TASK_DISPATCH`) and one handler
function. ASL editing is no longer part of the workflow.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from common.events import (
    EventEnvelope,
    RunCompleted,
)
from common.ids import CorrelationId, RunId, new_event_id
from common.state import (
    TERMINAL_RUN_STATES,
    TERMINAL_TASK_STATES,
    RunState,
    TaskState,
)
from state_router.actions import (
    Action,
    AdvanceState,
    CompoundAction,
    EmitEvent,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
    SeedTasks,
    WriteSyntheticSpec,
)

if TYPE_CHECKING:
    from state_router.model import Run, Task

type RunHandler = Callable[["Run"], Action]
type TaskHandler = Callable[["Run", "Task"], Action]


# ---------------------------------------------------------------------------
# Runtime ARN env-var helpers — read once per invocation, not at import.
# ---------------------------------------------------------------------------


def runtime_arn(name: str) -> str:
    """Read the runtime ARN env var for a named agent.

    All seven agent runtimes are passed in via env vars in the form
    ``AIDLC_{NAME}_RUNTIME_ARN``. Missing means the runtime hasn't been
    provisioned yet (bootstrap apply); the dispatch handler returns a
    Noop instead of dispatching, and the run sits until the next deploy
    completes the runtime ARNs.
    """
    return os.environ.get(f"AIDLC_{name.upper()}_RUNTIME_ARN", "")


def repo_helper_function_name() -> str:
    """Lambda function name for ``repo_helper`` invocations."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME", "")


def artifacts_bucket() -> str:
    """S3 bucket for artifacts (spec bundles, critiques, etc.)."""
    return os.environ.get("AIDLC_ARTIFACTS_BUCKET", "")


# ---------------------------------------------------------------------------
# Run-level handlers
# ---------------------------------------------------------------------------


def handle_received(run: Run) -> Action:
    """Branch on whether the run was triggered by a GitHub issue.

    Issue-driven runs go through triage first; programmatic runs (POST
    /v1/runs without ``source_issue_url``) skip straight to the
    architect.
    """
    arn = runtime_arn("triage" if run.source_issue_url else "architect")
    if not arn:
        return Noop("runtime ARN not yet provisioned")
    if run.source_issue_url:
        return invoke_triage(run, arn)
    return invoke_architect(run, arn, advance_from=RunState.received)


def invoke_triage(run: Run, arn: str) -> InvokeAgent:
    """Dispatch the triage agent and advance to ``triaging``."""
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-triage",
        payload={
            "project_slug": run.project_slug,
            "target_repo": run.target_repo,
            "issue_url": run.source_issue_url,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
            "requestor_sub": run.requestor_sub,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.received.value,
        advance_to=RunState.triaging.value,
    )


def invoke_architect(run: Run, arn: str, *, advance_from: RunState) -> InvokeAgent:
    """Dispatch the architect agent and advance to ``architect_running``."""
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-architect",
        payload={
            "project_slug": run.project_slug,
            "intent": run.intent,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=advance_from.value,
        advance_to=RunState.architect_running.value,
    )


def handle_triage_decided(run: Run) -> Action:
    """Branch on the triage's ``workflow_kind``.

    ``spec_driven`` → run the architect → critic → spec-PR flow.
    ``bug_fix`` / ``upgrade`` / ``docs`` → write a synthetic spec and
    skip straight to ``tasks_in_progress``.
    """
    if run.workflow_kind == "spec_driven" or run.workflow_kind is None:
        arn = runtime_arn("architect")
        if not arn:
            return Noop("architect runtime ARN not yet provisioned")
        return invoke_architect(run, arn, advance_from=RunState.triage_decided)
    if run.workflow_kind in {"bug_fix", "upgrade", "docs"}:
        return WriteSyntheticSpec(
            s3_key_prefix=f"specs/{run.synthetic_spec_slug or run.run_id}/",
            requirements_md=render_synthetic_requirements(run),
            design_md=render_synthetic_design(run),
            tasks_md=render_synthetic_tasks(run),
            target_pk=f"RUN#{run.run_id}",
            target_sk="STATE",
            advance_from=RunState.triage_decided.value,
            advance_to=RunState.tasks_in_progress.value,
        )
    return Noop(f"unknown workflow_kind: {run.workflow_kind}")


def handle_spec_pending(run: Run) -> Action:
    """Architect not yet dispatched — kick it off."""
    arn = runtime_arn("architect")
    if not arn:
        return Noop("architect runtime ARN not yet provisioned")
    return invoke_architect(run, arn, advance_from=RunState.spec_pending)


def handle_spec_drafted(run: Run) -> Action:
    """Architect produced a spec — dispatch the critic."""
    arn = runtime_arn("critic")
    if not arn:
        return Noop("critic runtime ARN not yet provisioned")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-critic",
        payload={
            "project_slug": run.project_slug,
            "spec_slug": run.spec_slug,
            "spec_s3_prefix": run.spec_s3_prefix,
            "intent": run.intent,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.spec_drafted.value,
        advance_to=RunState.critic_running.value,
    )


def handle_spec_critiqued(run: Run) -> Action:
    """Critic done — open the spec PR via repo_helper."""
    fn = repo_helper_function_name()
    if not fn or not run.spec_slug or not run.target_repo:
        return Noop("repo_helper or spec context not yet available")
    return InvokeRepoHelper(
        op="open_spec_pr",
        args={
            "repo": run.target_repo,
            "spec_slug": run.spec_slug,
            "spec_s3_prefix": run.spec_s3_prefix,
            "run_id": run.run_id,
            "requestor_sub": run.requestor_sub,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.spec_critiqued.value,
        advance_to=RunState.spec_pr_open.value,
        record_pr_url_attr="spec_pr_url",
    )


def handle_spec_approved(run: Run) -> Action:
    """Spec PR merged — seed task rows and advance to ``tasks_in_progress``.

    Without seeded TASK rows, ``tasks_in_progress`` is a permanent Noop
    (``handle_tasks_in_progress`` walks ``run.tasks`` and there's nothing
    to walk). The projector populated ``run.task_ids`` off the SPEC.READY
    event; if it's empty here, something earlier dropped the field —
    Noop and let an operator investigate rather than silently advancing
    to a dead-end state.
    """
    if not run.task_ids:
        return Noop("spec_approved with no task_ids — projector hasn't seeded them")
    return CompoundAction(
        actions=(
            SeedTasks(run_id=run.run_id, task_ids=run.task_ids),
            AdvanceState(
                target_pk=f"RUN#{run.run_id}",
                target_sk="STATE",
                advance_from=RunState.spec_approved.value,
                advance_to=RunState.tasks_in_progress.value,
            ),
        ),
    )


def handle_tasks_in_progress(run: Run) -> Action:
    """Walk task rows; dispatch any actionable, otherwise emit completion.

    Task-level dispatch returns one action per task. We collect them
    into a :class:`CompoundAction`. When every task is in a terminal
    state, the run transitions to ``tasks_complete`` (not done yet —
    the projector will apply ``RUN.COMPLETED → done``).
    """
    pending = [decide_task(run, t) for t in run.tasks]
    real_actions = tuple(a for a in pending if not isinstance(a, Noop))
    if not run.tasks:
        return Noop("no tasks seeded yet")
    if all(t.state in TERMINAL_TASK_STATES for t in run.tasks):
        return CompoundAction(
            actions=(
                AdvanceState(
                    target_pk=f"RUN#{run.run_id}",
                    target_sk="STATE",
                    advance_from=RunState.tasks_in_progress.value,
                    advance_to=RunState.tasks_complete.value,
                ),
            ),
        )
    if not real_actions:
        return Noop("all tasks are running or waiting")
    return CompoundAction(actions=real_actions)


def handle_tasks_complete(run: Run) -> Action:
    """Emit ``RUN.COMPLETED`` so the projector advances to ``done``."""
    completed = sum(1 for t in run.tasks if t.state == TaskState.merged)
    return EmitEvent(
        envelope=EventEnvelope[RunCompleted](
            event_id=new_event_id(),
            type="RUN.COMPLETED",
            run_id=RunId(run.run_id),
            correlation_id=CorrelationId(run.correlation_id),
            actor_id="state_router",
            payload=RunCompleted(
                project_slug=run.project_slug,
                spec_slug=run.spec_slug or "",
                tasks_completed=completed,
            ),
        ),
    )


def noop_waiting(run: Run) -> Action:
    """No-op for states that wait on an external event."""
    return Noop(f"waiting in {run.current_state}")


def terminal(run: Run) -> Action:
    """Terminal state — beacon should be deleted by the handler."""
    return Noop(f"terminal: {run.current_state}")


# ---------------------------------------------------------------------------
# Task-level handlers
# ---------------------------------------------------------------------------


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
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.pending.value,
        advance_to=TaskState.implementer_running.value,
    )


def dispatch_advisors(run: Run, task: Task) -> Action:
    """PR is open — fire reviewer + tester in parallel.

    Both advisors run independently; their ``REVIEW.READY`` and
    ``TEST_REPORT.READY`` events are advisory and do not change task
    state. The task immediately advances to ``pending_approval`` —
    advisors post their findings as PR comments while we wait for a
    human merge or PR-review verdict.
    """
    actions = []
    reviewer_arn = runtime_arn("reviewer")
    tester_arn = runtime_arn("tester")
    if reviewer_arn:
        actions.append(invoke_reviewer(run, task, reviewer_arn))
    if tester_arn:
        actions.append(invoke_tester(run, task, tester_arn))
    actions.append(
        AdvanceState(
            target_pk=f"RUN#{run.run_id}",
            target_sk=f"TASK#{task.task_id}",
            advance_from=TaskState.pr_open.value,
            advance_to=TaskState.pending_approval.value,
        ),
    )
    return CompoundAction(actions=tuple(actions))


def invoke_reviewer(run: Run, task: Task, arn: str) -> InvokeAgent:
    """Fire the reviewer against the task's PR."""
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
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.pr_open.value,
        # Reviewer runs in parallel with tester; advisors don't gate task
        # state. The advance is a placeholder — advisor invokes are
        # fire-and-forget alongside the AdvanceState in dispatch_advisors.
        advance_to=TaskState.pr_open.value,
    )


def invoke_tester(run: Run, task: Task, arn: str) -> InvokeAgent:
    """Fire the tester against the task's PR."""
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
        target_pk=f"RUN#{run.run_id}",
        target_sk=f"TASK#{task.task_id}",
        advance_from=TaskState.pr_open.value,
        advance_to=TaskState.pr_open.value,
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


# ---------------------------------------------------------------------------
# Synthetic spec rendering (formerly in triage_dispatcher)
# ---------------------------------------------------------------------------


def render_synthetic_requirements(run: Run) -> str:
    """Render a single-task requirements doc from the run's intent."""
    return (
        f"# Requirements\n\n"
        f"## Source\n\n"
        f"Auto-synthesized for `{run.workflow_kind}` workflow from "
        f"{run.source_issue_url or 'programmatic request'}.\n\n"
        f"## Intent\n\n{run.intent}\n"
    )


def render_synthetic_design(run: Run) -> str:
    """Render a minimal design doc that defers the design decisions to the implementer."""
    return (
        f"# Design\n\n"
        f"Workflow: `{run.workflow_kind}`. The implementer is expected to read the "
        f"target repo, understand existing structure, and make the smallest viable change.\n"
    )


def render_synthetic_tasks(run: Run) -> str:
    """Render a one-task tasks doc."""
    return (
        f"# Tasks\n\n"
        f"## T-001 — {run.workflow_kind}: address the request\n\n"
        f"Implement the change described in the requirements. Open one PR.\n"
    )


# ---------------------------------------------------------------------------
# Dispatch tables — public entry points used by the handler.
# ---------------------------------------------------------------------------


RUN_DISPATCH: Mapping[RunState, RunHandler] = {
    RunState.received: handle_received,
    RunState.triaging: noop_waiting,
    RunState.triage_decided: handle_triage_decided,
    RunState.spec_pending: handle_spec_pending,
    RunState.architect_running: noop_waiting,
    RunState.spec_drafted: handle_spec_drafted,
    RunState.critic_running: noop_waiting,
    RunState.spec_critiqued: handle_spec_critiqued,
    RunState.spec_pr_open: noop_waiting,
    RunState.spec_approved: handle_spec_approved,
    RunState.tasks_in_progress: handle_tasks_in_progress,
    RunState.tasks_complete: handle_tasks_complete,
    RunState.done: terminal,
    RunState.failed: terminal,
    RunState.cancelled: terminal,
}


TASK_DISPATCH: Mapping[TaskState, TaskHandler] = {
    TaskState.pending: dispatch_implementer,
    TaskState.implementer_running: task_noop_waiting,
    TaskState.pr_open: dispatch_advisors,
    TaskState.reviewer_running: task_noop_waiting,
    TaskState.tester_running: task_noop_waiting,
    TaskState.iterating: dispatch_iteration,
    TaskState.pending_approval: task_noop_waiting,
    TaskState.merged: task_terminal,
    TaskState.closed: task_terminal,
    TaskState.failed: task_terminal,
}


def decide(run: Run) -> Action:
    """Top-level dispatch: returns the next action for ``run``.

    Terminal runs return a Noop — the handler deletes the SQS beacon
    instead of executing. Unknown states are also Noop (defensive
    against a forgotten dispatch table entry).
    """
    if run.current_state is None:
        return Noop("current_state not yet set by projector")
    if run.current_state in TERMINAL_RUN_STATES:
        return terminal(run)
    handler = RUN_DISPATCH.get(run.current_state)
    if handler is None:
        return Noop(f"unknown run state: {run.current_state}")
    return handler(run)


__all__ = [
    "RUN_DISPATCH",
    "TASK_DISPATCH",
    "decide",
    "decide_task",
]
