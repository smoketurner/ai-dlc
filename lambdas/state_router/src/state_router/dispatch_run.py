"""Run-state dispatch — pure functions ``Run -> Action``.

Each handler decides the next action for one run state. No side
effects: the executors in :mod:`state_router.execute` consume the
returned actions and apply them.

The handler set is mostly 1:1 with :class:`~common.state.RunState`
entries; states that wait on external events map to
:func:`noop_waiting` and terminal states to :func:`terminal`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from common.events import (
    EventEnvelope,
    RunCancelRequested,
    RunCompleted,
)
from common.ids import CorrelationId, RunId, new_event_id
from common.state import (
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
from state_router.config import (
    repo_helper_function_name,
    runtime_arn,
)
from state_router.dispatch_task import decide_task
from state_router.synthetic_spec import (
    SYNTHETIC_TASK_ID,
    render_design,
    render_requirements,
    render_tasks,
)

if TYPE_CHECKING:
    from state_router.model import Run

type RunHandler = Callable[["Run"], Action]


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


def invoke_triage(run: Run, arn: str) -> Action:
    """Dispatch the triage agent and advance to ``triaging``.

    ``TriageInput`` requires every field below; if the row is missing
    the issue number/title/body/labels (e.g., the trigger preceded the
    issue-context plumbing), Noop and let an operator fix the row
    rather than dispatching an agent that will only ``ValidationError``.
    """
    if (
        not run.target_repo
        or not run.source_issue_url
        or run.issue_number is None
        or not run.issue_title
    ):
        return Noop("triage: STATE row missing issue context")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-triage",
        payload={
            "project_slug": run.project_slug,
            "target_repo": run.target_repo,
            "issue_url": run.source_issue_url,
            "issue_number": run.issue_number,
            "issue_title": run.issue_title,
            "issue_body": run.issue_body or "",
            "issue_labels": list(run.issue_labels),
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


def invoke_proposer_research(run: Run, arn: str, *, advance_from: RunState) -> InvokeAgent:
    """Dispatch the proposer for an issue-driven research run.

    The agent's research substrate is the issue body (which carries the
    URLs the user asked us to read). ``run.intent`` holds only the issue
    title — useful for a one-line preamble but not for fetching. We send
    ``title + body`` as the agent's ``intent`` so the URLs are visible
    to ``browse_url``-based extraction.
    """
    body = run.issue_body or ""
    title = run.intent or ""
    intent = f"{title}\n\n{body}".strip() if body else title
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-proposer",
        payload={
            "project_slug": run.project_slug,
            "target_repo": run.target_repo,
            "trigger_reason": "research",
            "intent": intent,
            "issue_number": run.issue_number,
            "triggering_comment_body": run.triggering_comment_body or "",
            "triggering_commenter": run.triggering_commenter or "",
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=advance_from.value,
        advance_to=RunState.proposer_running.value,
    )


def handle_triage_decided(run: Run) -> Action:
    """Branch on the triage's ``action`` (and ``workflow_kind`` for proceed).

    ``proceed`` + ``spec_driven`` → architect → critic → spec-PR flow.
    ``proceed`` + ``bug_fix``/``upgrade``/``docs`` → synthetic spec.
    ``ask`` → comment + label ``aidlc:awaiting-response`` on the issue;
    cancel this run (the webhook mints a fresh run on the user's reply).
    ``defer`` / ``decline`` → label the issue and cancel this run.
    """
    action = run.triage_action or "proceed"
    if action == "proceed":
        return handle_triage_proceed(run)
    if action == "ask":
        return triage_ask(run)
    if action == "defer":
        return triage_close(run, label="aidlc:deferred", reason="triage deferred")
    if action == "decline":
        return triage_close(run, label="aidlc:declined", reason="triage declined")
    return Noop(f"unknown triage action: {action}")


def handle_triage_proceed(run: Run) -> Action:
    """Triage said ``proceed`` — fork on workflow_kind."""
    if run.workflow_kind == "spec_driven" or run.workflow_kind is None:
        arn = runtime_arn("architect")
        if not arn:
            return Noop("architect runtime ARN not yet provisioned")
        return invoke_architect(run, arn, advance_from=RunState.triage_decided)
    if run.workflow_kind == "research":
        arn = runtime_arn("proposer")
        if not arn:
            return Noop("proposer runtime ARN not yet provisioned")
        return invoke_proposer_research(run, arn, advance_from=RunState.triage_decided)
    if run.workflow_kind in {"bug_fix", "upgrade", "docs"}:
        return CompoundAction(
            actions=(
                WriteSyntheticSpec(
                    s3_key_prefix=f"specs/{run.synthetic_spec_slug or run.run_id}/",
                    requirements_md=render_requirements(run),
                    design_md=render_design(run),
                    tasks_md=render_tasks(run),
                    target_pk=f"RUN#{run.run_id}",
                    target_sk="STATE",
                    advance_from=RunState.triage_decided.value,
                    advance_to=RunState.tasks_in_progress.value,
                ),
                # Synthetic specs always have one task; seed its row before
                # tasks_in_progress walks the (otherwise empty) task list.
                SeedTasks(run_id=run.run_id, task_ids=(SYNTHETIC_TASK_ID,)),
            ),
        )
    return Noop(f"unknown workflow_kind: {run.workflow_kind}")


def triage_ask(run: Run) -> Action:
    """Post a clarifying comment + ``aidlc:awaiting-response`` label, then cancel."""
    if not run.target_repo or run.issue_number is None:
        return Noop("triage ask: missing target_repo / issue_number")
    actions: list[Action] = [
        InvokeRepoHelper(
            op="comment_issue",
            args={
                "repo": run.target_repo,
                "issue_number": run.issue_number,
                "body": (
                    "Triage needs more information before I can start. "
                    "Reply with the missing details and add `/aidlc go` to retry."
                ),
            },
        ),
        InvokeRepoHelper(
            op="label_issue",
            args={
                "repo": run.target_repo,
                "issue_number": run.issue_number,
                "labels": ["aidlc:awaiting-response"],
            },
        ),
        emit_run_cancel(run, source="comment_command", reason="triage asked for clarification"),
    ]
    return CompoundAction(actions=tuple(actions))


def triage_close(run: Run, *, label: str, reason: str) -> Action:
    """Label the issue (defer / decline), then cancel the run."""
    if not run.target_repo or run.issue_number is None:
        return Noop(f"{reason}: missing target_repo / issue_number")
    actions: list[Action] = [
        InvokeRepoHelper(
            op="label_issue",
            args={
                "repo": run.target_repo,
                "issue_number": run.issue_number,
                "labels": [label],
            },
        ),
        emit_run_cancel(run, source="comment_command", reason=reason),
    ]
    return CompoundAction(actions=tuple(actions))


def emit_run_cancel(run: Run, *, source: str, reason: str) -> EmitEvent:
    """Build a ``RUN.CANCEL_REQUESTED`` envelope so the projector cancels the run."""
    return EmitEvent(
        envelope=EventEnvelope[RunCancelRequested](
            event_id=new_event_id(),
            type="RUN.CANCEL_REQUESTED",
            run_id=RunId(run.run_id),
            correlation_id=CorrelationId(run.correlation_id),
            actor_id="state_router",
            payload=RunCancelRequested(
                project_slug=run.project_slug,
                requestor=run.requestor,
                source=source,  # ty: ignore[invalid-argument-type]
                reason=reason,
            ),
        ),
    )


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
            # Issue-driven runs get a backlink in the spec PR body so
            # GitHub's UI cross-references the source issue. Programmatic
            # runs (POST /v1/runs) leave this None.
            "source_issue_url": run.source_issue_url,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.spec_critiqued.value,
        advance_to=RunState.spec_pr_open.value,
        record_pr_url_attr="pr_url",
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
    RunState.proposer_running: noop_waiting,
    RunState.tasks_complete: handle_tasks_complete,
    RunState.done: terminal,
    RunState.failed: terminal,
    RunState.cancelled: terminal,
}
