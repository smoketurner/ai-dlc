"""Tests for ``common.state``."""

from __future__ import annotations

from common.state import (
    TERMINAL_RUN_STATES,
    TERMINAL_TASK_STATES,
    RunState,
    TaskState,
)


def test_run_state_values_match_design() -> None:
    assert RunState.received == "received"
    assert RunState.triaging == "triaging"
    assert RunState.tasks_in_progress == "tasks_in_progress"
    assert RunState.done == "done"


def test_task_state_values_match_design() -> None:
    assert TaskState.pending == "pending"
    assert TaskState.implementer_running == "implementer_running"
    assert TaskState.iterating == "iterating"
    assert TaskState.merged == "merged"


def test_run_state_terminal_set_matches_doc() -> None:
    assert (
        frozenset(
            {RunState.done, RunState.failed, RunState.cancelled},
        )
        == TERMINAL_RUN_STATES
    )


def test_task_state_terminal_set_matches_doc() -> None:
    assert (
        frozenset(
            {TaskState.merged, TaskState.closed, TaskState.failed},
        )
        == TERMINAL_TASK_STATES
    )


def test_terminal_run_states_are_immutable() -> None:
    assert isinstance(TERMINAL_RUN_STATES, frozenset)


def test_terminal_task_states_are_immutable() -> None:
    assert isinstance(TERMINAL_TASK_STATES, frozenset)


def test_run_state_iterable_for_dispatch_table() -> None:
    every = list(RunState)
    assert RunState.received in every
    assert RunState.tasks_in_progress in every
    assert RunState.proposer_running in every
    assert RunState.done in every
    assert len(every) == 16


def test_task_state_iterable_for_dispatch_table() -> None:
    every = list(TaskState)
    assert TaskState.pending in every
    assert TaskState.iterating in every
    assert TaskState.blocked in every
    assert TaskState.merged in every
    assert len(every) == 11
