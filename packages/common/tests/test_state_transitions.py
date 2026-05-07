"""Tests for ``common.state_transitions``."""

from __future__ import annotations

import pytest

from common.state import RunState, TaskState
from common.state_transitions import (
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    apply_run_transition,
    apply_task_transition,
)


class TestRunTransitions:
    def test_request_received_from_none_to_received(self) -> None:
        assert (
            apply_run_transition(
                event_type="REQUEST.RECEIVED",
                current_state=None,
            )
            == RunState.received
        )

    def test_issue_triaged_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="ISSUE.TRIAGED",
                current_state=RunState.triaging,
            )
            == RunState.triage_decided
        )

    def test_spec_ready_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="SPEC.READY",
                current_state=RunState.architect_running,
            )
            == RunState.spec_drafted
        )

    def test_critique_ready_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="CRITIQUE.READY",
                current_state=RunState.critic_running,
            )
            == RunState.spec_critiqued
        )

    def test_spec_approved_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="SPEC.APPROVED",
                current_state=RunState.spec_pr_open,
            )
            == RunState.spec_approved
        )

    def test_spec_rejected_fails_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="SPEC.REJECTED",
                current_state=RunState.spec_pr_open,
            )
            == RunState.failed
        )

    def test_run_completed_advances_to_done(self) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.COMPLETED",
                current_state=RunState.tasks_complete,
            )
            == RunState.done
        )


class TestRunWildcards:
    @pytest.mark.parametrize(
        "current",
        [
            RunState.received,
            RunState.triaging,
            RunState.architect_running,
            RunState.tasks_in_progress,
            RunState.spec_pr_open,
        ],
    )
    def test_run_failed_advances_any_non_terminal_to_failed(
        self,
        current: RunState,
    ) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.FAILED",
                current_state=current,
            )
            == RunState.failed
        )

    @pytest.mark.parametrize(
        "current",
        [RunState.done, RunState.failed, RunState.cancelled],
    )
    def test_run_failed_no_op_on_terminal(self, current: RunState) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.FAILED",
                current_state=current,
            )
            is None
        )

    @pytest.mark.parametrize(
        "current",
        [
            RunState.received,
            RunState.triaging,
            RunState.spec_pr_open,
            RunState.tasks_in_progress,
        ],
    )
    def test_cancel_requested_advances_any_non_terminal_to_cancelled(
        self,
        current: RunState,
    ) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.CANCEL_REQUESTED",
                current_state=current,
            )
            == RunState.cancelled
        )

    @pytest.mark.parametrize(
        "current",
        [RunState.done, RunState.failed, RunState.cancelled],
    )
    def test_cancel_requested_no_op_on_terminal(self, current: RunState) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.CANCEL_REQUESTED",
                current_state=current,
            )
            is None
        )


class TestRunInvalidTransitions:
    def test_unknown_event_returns_none(self) -> None:
        assert (
            apply_run_transition(
                event_type="REVIEW.READY",
                current_state=RunState.tasks_in_progress,
            )
            is None
        )

    def test_event_from_wrong_state_returns_none(self) -> None:
        assert (
            apply_run_transition(
                event_type="SPEC.READY",
                current_state=RunState.received,
            )
            is None
        )

    def test_eval_drift_does_not_advance_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="EVAL.DRIFT_DETECTED",
                current_state=RunState.tasks_in_progress,
            )
            is None
        )

    def test_re_delivered_request_received_after_advance_is_no_op(self) -> None:
        # The projector should see (REQUEST.RECEIVED, current=received)
        # on a duplicate delivery and return None.
        assert (
            apply_run_transition(
                event_type="REQUEST.RECEIVED",
                current_state=RunState.received,
            )
            is None
        )


class TestTaskTransitions:
    def test_task_ready_from_implementer_running(self) -> None:
        assert (
            apply_task_transition(
                event_type="TASK.READY",
                current_state=TaskState.implementer_running,
            )
            == TaskState.pr_open
        )

    def test_task_ready_from_iterating(self) -> None:
        # Iteration commit re-emits TASK.READY against an iterating task.
        assert (
            apply_task_transition(
                event_type="TASK.READY",
                current_state=TaskState.iterating,
            )
            == TaskState.pr_open
        )

    def test_iteration_requested_from_pr_open(self) -> None:
        assert (
            apply_task_transition(
                event_type="TASK.ITERATION_REQUESTED",
                current_state=TaskState.pr_open,
            )
            == TaskState.iterating
        )

    def test_iteration_requested_from_pending_approval(self) -> None:
        # A reviewer can change_requested while the task is in pending_approval.
        assert (
            apply_task_transition(
                event_type="TASK.ITERATION_REQUESTED",
                current_state=TaskState.pending_approval,
            )
            == TaskState.iterating
        )

    def test_task_approved_to_merged(self) -> None:
        assert (
            apply_task_transition(
                event_type="TASK.APPROVED",
                current_state=TaskState.pending_approval,
            )
            == TaskState.merged
        )

    def test_task_rejected_to_closed(self) -> None:
        assert (
            apply_task_transition(
                event_type="TASK.REJECTED",
                current_state=TaskState.pending_approval,
            )
            == TaskState.closed
        )


class TestTaskInvalidTransitions:
    @pytest.mark.parametrize(
        "current",
        [TaskState.merged, TaskState.closed, TaskState.failed],
    )
    def test_terminal_task_states_never_transition(self, current: TaskState) -> None:
        # Even a re-delivered TASK.APPROVED from a merged task is a no-op.
        assert (
            apply_task_transition(
                event_type="TASK.APPROVED",
                current_state=current,
            )
            is None
        )

    def test_review_ready_does_not_advance_task(self) -> None:
        # Advisory event — should not transition state.
        assert (
            apply_task_transition(
                event_type="REVIEW.READY",
                current_state=TaskState.reviewer_running,
            )
            is None
        )

    def test_test_report_ready_does_not_advance_task(self) -> None:
        # Advisory event — should not transition state.
        assert (
            apply_task_transition(
                event_type="TEST_REPORT.READY",
                current_state=TaskState.tester_running,
            )
            is None
        )

    def test_iteration_requested_from_implementer_running_no_op(self) -> None:
        # While implementer is in flight, an iteration request is queued
        # via delivery_ids/pending feedback but does not change state.
        assert (
            apply_task_transition(
                event_type="TASK.ITERATION_REQUESTED",
                current_state=TaskState.implementer_running,
            )
            is None
        )


class TestTransitionTablesShape:
    def test_run_transitions_targets_are_run_states(self) -> None:
        for target in RUN_TRANSITIONS.values():
            assert isinstance(target, RunState)

    def test_task_transitions_targets_are_task_states(self) -> None:
        for target in TASK_TRANSITIONS.values():
            assert isinstance(target, TaskState)

    def test_no_transition_creates_a_self_loop(self) -> None:
        for (_event, current), target in RUN_TRANSITIONS.items():
            assert target != current, f"self-loop for run {current}"
        for (_event, current), target in TASK_TRANSITIONS.items():
            assert target != current, f"self-loop for task {current}"
