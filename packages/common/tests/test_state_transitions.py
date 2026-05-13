"""Tests for ``common.state_transitions``."""

from __future__ import annotations

import pytest

import common.state_transitions as st_module
from common.state import RunState
from common.state_transitions import (
    RUN_TRANSITIONS,
    apply_run_transition,
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

    def test_design_ready_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="DESIGN.READY",
                current_state=RunState.architect_running,
            )
            == RunState.designed
        )

    def test_critique_ready_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="CRITIQUE.READY",
                current_state=RunState.critic_running,
            )
            == RunState.critiqued
        )

    def test_impl_pr_opened_advances_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="IMPL_PR.OPENED",
                current_state=RunState.implementer_running,
            )
            == RunState.impl_pr_open
        )

    def test_review_ready_advances_validation_running_to_complete(self) -> None:
        assert (
            apply_run_transition(
                event_type="REVIEW.READY",
                current_state=RunState.validation_running,
            )
            == RunState.validation_complete
        )

    def test_revision_ready_advances_revising_back_to_validation_running(self) -> None:
        assert (
            apply_run_transition(
                event_type="REVISION.READY",
                current_state=RunState.revising,
            )
            == RunState.validation_running
        )

    def test_checks_passed_from_validation_complete(self) -> None:
        assert (
            apply_run_transition(
                event_type="CHECKS.PASSED",
                current_state=RunState.validation_complete,
            )
            == RunState.awaiting_human_merge
        )

    def test_checks_passed_from_awaiting_checks(self) -> None:
        assert (
            apply_run_transition(
                event_type="CHECKS.PASSED",
                current_state=RunState.awaiting_checks,
            )
            == RunState.awaiting_human_merge
        )

    def test_checks_failed_from_validation_complete(self) -> None:
        assert (
            apply_run_transition(
                event_type="CHECKS.FAILED",
                current_state=RunState.validation_complete,
            )
            == RunState.revising
        )

    def test_checks_failed_from_awaiting_checks(self) -> None:
        assert (
            apply_run_transition(
                event_type="CHECKS.FAILED",
                current_state=RunState.awaiting_checks,
            )
            == RunState.revising
        )

    def test_checks_failed_from_awaiting_human_merge(self) -> None:
        assert (
            apply_run_transition(
                event_type="CHECKS.FAILED",
                current_state=RunState.awaiting_human_merge,
            )
            == RunState.revising
        )

    def test_impl_iteration_requested_from_awaiting_checks(self) -> None:
        assert (
            apply_run_transition(
                event_type="IMPL.ITERATION_REQUESTED",
                current_state=RunState.awaiting_checks,
            )
            == RunState.revising
        )

    def test_impl_iteration_requested_from_awaiting_human_merge(self) -> None:
        assert (
            apply_run_transition(
                event_type="IMPL.ITERATION_REQUESTED",
                current_state=RunState.awaiting_human_merge,
            )
            == RunState.revising
        )

    def test_impl_iteration_requested_from_impl_pr_open(self) -> None:
        assert (
            apply_run_transition(
                event_type="IMPL.ITERATION_REQUESTED",
                current_state=RunState.impl_pr_open,
            )
            == RunState.revising
        )

    def test_run_completed_advances_to_done_from_awaiting_human_merge(self) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.COMPLETED",
                current_state=RunState.awaiting_human_merge,
            )
            == RunState.done
        )

    def test_run_completed_advances_to_done_from_proposer_running(self) -> None:
        assert (
            apply_run_transition(
                event_type="RUN.COMPLETED",
                current_state=RunState.proposer_running,
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
            RunState.implementer_running,
            RunState.impl_pr_open,
            RunState.validation_running,
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
            RunState.implementer_running,
            RunState.impl_pr_open,
            RunState.awaiting_human_merge,
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
                current_state=RunState.architect_running,
            )
            is None
        )

    def test_event_from_wrong_state_returns_none(self) -> None:
        assert (
            apply_run_transition(
                event_type="DESIGN.READY",
                current_state=RunState.received,
            )
            is None
        )

    def test_eval_drift_does_not_advance_run(self) -> None:
        assert (
            apply_run_transition(
                event_type="EVAL.DRIFT_DETECTED",
                current_state=RunState.validation_running,
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


class TestTransitionTableShape:
    def test_run_transitions_targets_are_run_states(self) -> None:
        for target in RUN_TRANSITIONS.values():
            assert isinstance(target, RunState)

    def test_no_transition_creates_a_self_loop(self) -> None:
        for (_event, current), target in RUN_TRANSITIONS.items():
            assert target != current, f"self-loop for run {current}"

    def test_no_task_transitions_exported(self) -> None:
        """The task-level state machine was removed in the single-PR refactor."""
        assert not hasattr(st_module, "TASK_TRANSITIONS")
        assert not hasattr(st_module, "apply_task_transition")
