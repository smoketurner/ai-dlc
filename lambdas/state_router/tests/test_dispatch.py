"""Pure-function tests for the state_router dispatch table.

Every state in :data:`RunState` and :data:`TaskState` has a row here.
A regression that drops a dispatch entry surfaces as an unmapped state
=> ``decide`` returns a ``Noop`` carrying ``unknown run state`` /
``unknown task state``; the corresponding ``test_*_returns_*`` would
fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from common.events import RunCompleted
from common.state import RunState, TaskState
from state_router.actions import (
    AdvanceState,
    CompoundAction,
    EmitEvent,
    GuardedAdvance,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
    SeedTasks,
    WriteSyntheticSpec,
)
from state_router.dispatch import RUN_DISPATCH, TASK_DISPATCH, decide, decide_task
from state_router.model import Run, Task


def make_run(  # noqa: PLR0913
    *,
    state: RunState | None,
    workflow_kind: str | None = "spec_driven",
    triage_action: str | None = None,
    source_issue_url: str | None = None,
    issue_number: int | None = None,
    issue_title: str | None = None,
    issue_body: str | None = None,
    issue_labels: tuple[str, ...] = (),
    spec_slug: str | None = None,
    spec_s3_prefix: str | None = None,
    target_repo: str | None = "owner/repo",
    task_ids: tuple[str, ...] = (),
    tasks: tuple[Task, ...] = (),
) -> Run:
    """Build a Run with sane defaults for tests."""
    return Run(
        run_id="r-1",
        correlation_id="c-1",
        project_slug="demo",
        intent="x",
        requestor="alice",
        actor_id="alice",
        current_state=state,
        workflow_kind=workflow_kind,
        triage_action=triage_action,
        target_repo=target_repo,
        source_issue_url=source_issue_url,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        issue_labels=issue_labels,
        spec_slug=spec_slug,
        spec_s3_prefix=spec_s3_prefix,
        task_ids=task_ids,
        tasks=tasks,
    )


def make_task(state: TaskState, **overrides: Any) -> Task:
    """Build a Task with sane defaults."""
    base: dict[str, Any] = {
        "task_id": "T-001",
        "state": state,
        "pr_url": None,
        "pr_number": None,
        "iteration_count": 0,
        "delivery_ids": frozenset(),
        "pending_feedback": (),
    }
    base.update(overrides)
    return Task(**base)


# ---------------------------------------------------------------------------
# Run-level dispatch
# ---------------------------------------------------------------------------


class TestRunReceived:
    def test_received_with_issue_invokes_triage(self) -> None:
        run = make_run(
            state=RunState.received,
            source_issue_url="https://github.com/o/r/issues/1",
            issue_number=1,
            issue_title="bug: foo",
            issue_body="describe",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "triage" in action.runtime_arn
        assert action.advance_to == RunState.triaging.value
        assert action.payload["issue_number"] == 1
        assert action.payload["issue_title"] == "bug: foo"
        assert action.payload["issue_body"] == "describe"

    def test_received_with_issue_url_only_is_noop(self) -> None:
        run = make_run(
            state=RunState.received,
            source_issue_url="https://github.com/o/r/issues/1",
        )
        action = decide(run)
        assert isinstance(action, Noop)

    def test_received_without_issue_invokes_architect(self) -> None:
        run = make_run(state=RunState.received)
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "architect" in action.runtime_arn
        assert action.advance_to == RunState.architect_running.value

    def test_received_noop_when_runtime_arn_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AIDLC_ARCHITECT_RUNTIME_ARN")
        run = make_run(state=RunState.received)
        action = decide(run)
        assert isinstance(action, Noop)


class TestRunWaitingStates:
    @pytest.mark.parametrize(
        "state",
        [
            RunState.triaging,
            RunState.architect_running,
            RunState.critic_running,
            RunState.spec_pr_open,
        ],
    )
    def test_waiting_states_return_noop(self, state: RunState) -> None:
        run = make_run(state=state)
        action = decide(run)
        assert isinstance(action, Noop)


class TestRunTriageDecided:
    def test_spec_driven_invokes_architect(self) -> None:
        run = make_run(state=RunState.triage_decided, workflow_kind="spec_driven")
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "architect" in action.runtime_arn

    def test_bug_fix_writes_synthetic_spec_and_seeds_task(self) -> None:
        run = make_run(state=RunState.triage_decided, workflow_kind="bug_fix")
        action = decide(run)
        assert isinstance(action, CompoundAction)
        synthetics = [a for a in action.actions if isinstance(a, WriteSyntheticSpec)]
        seeds = [a for a in action.actions if isinstance(a, SeedTasks)]
        assert len(synthetics) == 1
        assert "Requirements" in synthetics[0].requirements_md
        assert synthetics[0].advance_to == RunState.tasks_in_progress.value
        assert len(seeds) == 1
        assert seeds[0].task_ids == ("T-001",)

    @pytest.mark.parametrize("kind", ["upgrade", "docs"])
    def test_other_synthetic_kinds_write_spec(self, kind: str) -> None:
        run = make_run(state=RunState.triage_decided, workflow_kind=kind)
        action = decide(run)
        assert isinstance(action, CompoundAction)
        assert any(isinstance(a, WriteSyntheticSpec) for a in action.actions)
        assert any(isinstance(a, SeedTasks) for a in action.actions)

    def test_research_invokes_proposer(self) -> None:
        run = make_run(
            state=RunState.triage_decided,
            workflow_kind="research",
            issue_body="please review:\n- https://example.com/post-a\n- https://example.com/post-b",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "proposer" in action.runtime_arn
        assert action.payload["trigger_reason"] == "research"
        assert action.advance_to == RunState.proposer_running.value
        # The agent must see the URLs from the body, not just the title.
        assert "https://example.com/post-a" in action.payload["intent"]
        assert "https://example.com/post-b" in action.payload["intent"]


class TestRunSpecFlow:
    def test_spec_pending_invokes_architect(self) -> None:
        run = make_run(state=RunState.spec_pending)
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "architect" in action.runtime_arn

    def test_spec_drafted_invokes_critic(self) -> None:
        run = make_run(state=RunState.spec_drafted, spec_slug="demo")
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "critic" in action.runtime_arn

    def test_spec_critiqued_opens_pr_via_repo_helper(self) -> None:
        run = make_run(state=RunState.spec_critiqued, spec_slug="demo")
        action = decide(run)
        assert isinstance(action, InvokeRepoHelper)
        assert action.op == "open_spec_pr"
        assert action.advance_to == RunState.spec_pr_open.value

    def test_spec_critiqued_noop_without_target_repo(self) -> None:
        run = make_run(state=RunState.spec_critiqued, spec_slug="demo", target_repo=None)
        action = decide(run)
        assert isinstance(action, Noop)

    def test_spec_approved_seeds_tasks_and_advances(self) -> None:
        run = make_run(
            state=RunState.spec_approved,
            spec_slug="demo",
            task_ids=("T-001", "T-002"),
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        seeds = [a for a in action.actions if isinstance(a, SeedTasks)]
        advances = [a for a in action.actions if isinstance(a, AdvanceState)]
        assert len(seeds) == 1
        assert seeds[0].task_ids == ("T-001", "T-002")
        assert len(advances) == 1
        assert advances[0].advance_to == RunState.tasks_in_progress.value

    def test_spec_approved_with_no_task_ids_is_noop(self) -> None:
        run = make_run(state=RunState.spec_approved, spec_slug="demo")
        action = decide(run)
        assert isinstance(action, Noop)


class TestRunTasksInProgress:
    def test_no_tasks_seeded_yet_is_noop(self) -> None:
        run = make_run(state=RunState.tasks_in_progress)
        action = decide(run)
        assert isinstance(action, Noop)

    def test_all_terminal_tasks_advance_to_complete(self) -> None:
        run = make_run(
            state=RunState.tasks_in_progress,
            tasks=(make_task(TaskState.merged),),
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        assert any(
            isinstance(a, AdvanceState) and a.advance_to == RunState.tasks_complete.value
            for a in action.actions
        )

    def test_mixed_states_dispatch_only_actionable(self) -> None:
        run = make_run(
            state=RunState.tasks_in_progress,
            tasks=(
                make_task(TaskState.pending),
                make_task(TaskState.implementer_running, task_id="T-002"),
            ),
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        invokes = [a for a in action.actions if isinstance(a, InvokeAgent)]
        assert len(invokes) == 1
        assert "implementer" in invokes[0].runtime_arn


class TestRunTasksComplete:
    def test_emits_run_completed(self) -> None:
        run = make_run(
            state=RunState.tasks_complete,
            spec_slug="demo",
            tasks=(make_task(TaskState.merged), make_task(TaskState.merged, task_id="T-002")),
        )
        action = decide(run)
        assert isinstance(action, EmitEvent)
        assert action.envelope.type == "RUN.COMPLETED"
        assert isinstance(action.envelope.payload, RunCompleted)
        assert action.envelope.payload.tasks_completed == 2


class TestRunTerminalStates:
    @pytest.mark.parametrize(
        "state",
        [RunState.done, RunState.failed, RunState.cancelled],
    )
    def test_terminal_states_are_noop(self, state: RunState) -> None:
        run = make_run(state=state)
        action = decide(run)
        assert isinstance(action, Noop)

    def test_unset_state_is_noop(self) -> None:
        run = make_run(state=None)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# Task-level dispatch
# ---------------------------------------------------------------------------


class TestTaskDispatch:
    def test_pending_dispatches_implementer(self) -> None:
        run = make_run(state=RunState.tasks_in_progress, spec_slug="demo")
        action = decide_task(run, make_task(TaskState.pending))
        assert isinstance(action, InvokeAgent)
        assert "implementer" in action.runtime_arn
        assert action.advance_to == TaskState.implementer_running.value

    def test_pr_open_dispatches_advisors(self) -> None:
        run = make_run(state=RunState.tasks_in_progress, spec_slug="demo")
        task = make_task(TaskState.pr_open, pr_url="https://github.com/o/r/pull/1")
        action = decide_task(run, task)
        # The advisors are gated behind a single GuardedAdvance flipping
        # pr_open → pending_approval; only the winning router fires them.
        assert isinstance(action, GuardedAdvance)
        assert action.advance_from == TaskState.pr_open.value
        assert action.advance_to == TaskState.pending_approval.value
        invokes = [a for a in action.on_success if isinstance(a, InvokeAgent)]
        runtimes = [a.runtime_arn for a in invokes]
        assert any("reviewer" in arn for arn in runtimes)
        assert any("tester" in arn for arn in runtimes)
        # Each gated invoke fires unconditionally — the gate is the race guard.
        assert all(a.advance_from is None and a.advance_to is None for a in invokes)

    def test_iterating_dispatches_implementer_with_feedback(self) -> None:
        run = make_run(state=RunState.tasks_in_progress, spec_slug="demo")
        task = make_task(
            TaskState.iterating,
            pr_url="https://github.com/o/r/pull/1",
            iteration_count=1,
            pending_feedback=({"kind": "ci_failure", "workflow_name": "ci"},),
        )
        action = decide_task(run, task)
        assert isinstance(action, InvokeAgent)
        assert action.payload["iteration_count"] == 2
        assert action.payload["iteration_feedback"]
        assert action.advance_to == TaskState.implementer_running.value

    @pytest.mark.parametrize(
        "state",
        [
            TaskState.implementer_running,
            TaskState.reviewer_running,
            TaskState.tester_running,
            TaskState.pending_approval,
            # ``blocked`` is a wait-for-human state — the implementer
            # opened a draft PR with BLOCKED.md and the run sits until a
            # webhook lands TASK.ITERATION_REQUESTED (comment) or
            # TASK.REJECTED (close). Reviewer/Tester intentionally don't
            # fire because there's no implementation in the PR yet.
            TaskState.blocked,
        ],
    )
    def test_waiting_task_states_are_noop(self, state: TaskState) -> None:
        run = make_run(state=RunState.tasks_in_progress)
        action = decide_task(run, make_task(state))
        assert isinstance(action, Noop)

    @pytest.mark.parametrize(
        "state",
        [TaskState.merged, TaskState.closed, TaskState.failed],
    )
    def test_terminal_task_states_are_noop(self, state: TaskState) -> None:
        run = make_run(state=RunState.tasks_in_progress)
        action = decide_task(run, make_task(state))
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# Coverage guard
# ---------------------------------------------------------------------------


class TestDispatchTablesCover:
    def test_every_run_state_has_a_dispatch_handler(self) -> None:
        # Every RunState must be in the table — no silent gaps.
        for state in RunState:
            assert state in RUN_DISPATCH, f"RunState.{state.name} missing"

    def test_every_task_state_has_a_dispatch_handler(self) -> None:
        for state in TaskState:
            assert state in TASK_DISPATCH, f"TaskState.{state.name} missing"
