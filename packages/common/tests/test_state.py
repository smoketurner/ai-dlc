"""Tests for ``common.state``."""

from __future__ import annotations

import common.state as state_module
from common.state import (
    TERMINAL_RUN_STATES,
    RunState,
)


def test_run_state_values_match_design() -> None:
    assert RunState.received == "received"
    assert RunState.triaging == "triaging"
    assert RunState.triage_decided == "triage_decided"
    assert RunState.architect_running == "architect_running"
    assert RunState.designed == "designed"
    assert RunState.critic_running == "critic_running"
    assert RunState.critiqued == "critiqued"
    assert RunState.implementer_running == "implementer_running"
    assert RunState.impl_pr_open == "impl_pr_open"
    assert RunState.validation_running == "validation_running"
    assert RunState.validation_complete == "validation_complete"
    assert RunState.revising == "revising"
    assert RunState.awaiting_checks == "awaiting_checks"
    assert RunState.awaiting_human_merge == "awaiting_human_merge"
    assert RunState.done == "done"


def test_run_state_terminal_set_matches_doc() -> None:
    assert (
        frozenset(
            {RunState.done, RunState.failed, RunState.cancelled},
        )
        == TERMINAL_RUN_STATES
    )


def test_terminal_run_states_are_immutable() -> None:
    assert isinstance(TERMINAL_RUN_STATES, frozenset)


def test_run_state_iterable_for_dispatch_table() -> None:
    every = list(RunState)
    assert RunState.received in every
    assert RunState.validation_running in every
    assert RunState.validation_complete in every
    assert RunState.revising in every
    assert RunState.awaiting_human_merge in every
    assert RunState.awaiting_checks in every
    assert RunState.impl_pr_open in every
    assert RunState.designed in every
    assert RunState.critiqued in every
    assert RunState.proposer_running in every
    assert RunState.done in every


def test_no_task_state_exported() -> None:
    """The TaskState enum was removed in the single-PR-per-issue refactor."""
    assert not hasattr(state_module, "TaskState")
    assert not hasattr(state_module, "TERMINAL_TASK_STATES")
