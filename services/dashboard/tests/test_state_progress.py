"""Unit tests for ``dashboard.state_progress`` derivation helpers."""

from __future__ import annotations

from common.state import TERMINAL_RUN_STATES, RunState
from common.state_transitions import RUN_TRANSITIONS
from dashboard.state_progress import (
    agent_label,
    is_active,
    next_steps,
    progress_dict,
    stuck_threshold_seconds,
)


def test_every_running_state_has_a_label() -> None:
    """Every ``*_running`` and wait state surfaces a human-readable agent name."""
    must_have = {
        RunState.triaging,
        RunState.architect_running,
        RunState.critic_running,
        RunState.implementer_running,
        RunState.validation_running,
        RunState.revising,
        RunState.proposer_running,
        RunState.awaiting_checks,
        RunState.awaiting_human_merge,
    }
    for state in must_have:
        assert agent_label(state), f"{state} should have an agent_label"
        assert is_active(state)


def test_steady_cursor_states_are_inactive() -> None:
    """Projector-only cursor states render no in-flight panel."""
    for state in (
        RunState.received,
        RunState.triage_decided,
        RunState.designed,
        RunState.critiqued,
        RunState.impl_pr_open,
        RunState.validation_complete,
    ):
        assert agent_label(state) is None
        assert not is_active(state)


def test_terminal_states_are_inactive() -> None:
    for state in TERMINAL_RUN_STATES:
        assert agent_label(state) is None
        assert not is_active(state)


def test_none_state_is_inactive() -> None:
    assert agent_label(None) is None
    assert not is_active(None)
    assert next_steps(None) == []
    assert progress_dict(None, updated_at="2026-05-13T12:00:00Z") is None


def test_next_steps_mirrors_run_transitions() -> None:
    """Every (event, current_state) → next in RUN_TRANSITIONS is reachable."""
    for (event, current), nxt in RUN_TRANSITIONS.items():
        if current is None:
            continue
        assert (event, nxt) in next_steps(current), (
            f"missing inverse for {event}@{current}"
        )


def test_next_steps_excludes_wildcards() -> None:
    """Wildcard abort events (RUN.FAILED / RUN.CANCEL_REQUESTED) aren't surfaced."""
    for state in (
        RunState.architect_running,
        RunState.implementer_running,
        RunState.validation_running,
        RunState.awaiting_human_merge,
    ):
        events = {event for event, _ in next_steps(state)}
        assert "RUN.FAILED" not in events
        assert "RUN.CANCEL_REQUESTED" not in events


def test_validation_complete_has_no_label_but_has_next_steps() -> None:
    """validation_complete is a steady cursor with multiple successor paths."""
    assert agent_label(RunState.validation_complete) is None
    events = {event for event, _ in next_steps(RunState.validation_complete)}
    assert "CHECKS.PASSED" in events
    assert "CHECKS.FAILED" in events


def test_stuck_threshold_higher_for_human_wait() -> None:
    """Waits on humans / CI tolerate longer dwell than active agent work."""
    agent = stuck_threshold_seconds(RunState.implementer_running)
    human = stuck_threshold_seconds(RunState.awaiting_human_merge)
    checks = stuck_threshold_seconds(RunState.awaiting_checks)
    assert agent is not None and human is not None and checks is not None
    assert human > agent
    assert checks > agent
    assert stuck_threshold_seconds(RunState.designed) is None


def test_progress_dict_payload_shape() -> None:
    payload = progress_dict(RunState.architect_running, updated_at="2026-05-13T12:00:00Z")
    assert payload is not None
    assert payload["agent"] == "Architect"
    assert payload["state"] == "architect_running"
    assert payload["since"] == "2026-05-13T12:00:00Z"
    assert payload["stuck_threshold_seconds"] == 15 * 60
    expected_next = payload["expected_next"]
    assert isinstance(expected_next, list) and expected_next
    assert {"event": "DESIGN.READY", "state": "designed"} in expected_next


def test_progress_dict_validation_running_fanout_label() -> None:
    payload = progress_dict(RunState.validation_running, updated_at=None)
    assert payload is not None
    assert payload["agent"] == "Reviewer + Tester + Code-Critic"
    assert payload["since"] is None
