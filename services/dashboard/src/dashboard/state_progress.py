"""Derive in-flight progress signals from the run-state cursor.

Pure functions — no I/O. Given the current ``RunState`` (and optionally
``updated_at`` to compute elapsed-since-transition), produce:

* ``agent_label`` — human-readable name of the agent (or wait-on
  party) responsible for advancing the run from this state.
* ``next_steps`` — the ``(event, next_state)`` pairs that advance from
  here, derived by inverting :data:`common.state_transitions.RUN_TRANSITIONS`.
* ``stuck_threshold_seconds`` — when this many seconds elapse in the
  same state without a transition, the dashboard surfaces an amber
  "looks stuck" indicator.

``updated_at`` on the STATE row is bumped on every write (state advance
plus same-state usage rollups), so the elapsed value is a coarse "≥"
bound on time-in-state rather than a precise transition timestamp.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Final

from common.events import EventType
from common.state import RunState
from common.state_transitions import RUN_TRANSITIONS

_AGENT_LABELS: Final[Mapping[RunState, str]] = {
    RunState.triaging: "Triage",
    RunState.architect_running: "Architect",
    RunState.critic_running: "Critic",
    RunState.implementer_running: "Implementer",
    RunState.validation_running: "Reviewer + Tester + Code-Critic",
    RunState.revising: "Implementer (revision)",
    RunState.proposer_running: "Proposer",
    RunState.awaiting_checks: "GitHub Checks",
    RunState.awaiting_human_merge: "Human reviewer",
}

_WAIT_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.awaiting_checks, RunState.awaiting_human_merge},
)

_AGENT_RUNNING_THRESHOLD_S: Final[int] = 15 * 60
_HUMAN_WAIT_THRESHOLD_S: Final[int] = 30 * 60


def agent_label(state: RunState | None) -> str | None:
    """Display name of the party working from ``state``, or ``None``.

    Returns ``None`` for steady-cursor states (``received``,
    ``triage_decided``, ``designed``, ``critiqued``, ``impl_pr_open``,
    ``validation_complete``) and terminal states — the system is
    between transitions, not actively working.
    """
    if state is None:
        return None
    return _AGENT_LABELS.get(state)


def is_active(state: RunState | None) -> bool:
    """``True`` when ``state`` has an associated agent or wait party."""
    return agent_label(state) is not None


def next_steps(state: RunState | None) -> list[tuple[EventType, RunState]]:
    """Events that advance ``state`` and the resulting next state.

    Inverts :data:`RUN_TRANSITIONS` on ``current_state``. Wildcard
    transitions (``RUN.FAILED`` / ``RUN.CANCEL_REQUESTED``) are
    intentionally excluded — they're abort paths, not "what's expected".
    """
    if state is None:
        return []
    return _NEXT_STEPS.get(state, [])


def stuck_threshold_seconds(state: RunState | None) -> int | None:
    """Seconds of in-state dwell after which the UI flags the run as stuck."""
    if state is None or state not in _AGENT_LABELS:
        return None
    if state in _WAIT_STATES:
        return _HUMAN_WAIT_THRESHOLD_S
    return _AGENT_RUNNING_THRESHOLD_S


def progress_dict(
    state: RunState | None,
    *,
    updated_at: str | None,
) -> dict[str, object] | None:
    """Build a JSON-serialisable payload for the "currently running" panel.

    Returns ``None`` when ``state`` has no active agent (steady-cursor
    or terminal). ``updated_at`` is passed through verbatim as the
    "in-state since" anchor; the client formats elapsed time.
    """
    label = agent_label(state)
    if label is None or state is None:
        return None
    return {
        "agent": label,
        "state": state.value,
        "since": updated_at,
        "stuck_threshold_seconds": stuck_threshold_seconds(state),
        "expected_next": [
            {"event": event, "state": next_state.value} for event, next_state in next_steps(state)
        ],
    }


def _build_next_steps() -> dict[RunState, list[tuple[EventType, RunState]]]:
    """Invert ``RUN_TRANSITIONS`` once at import time."""
    inverse: dict[RunState, list[tuple[EventType, RunState]]] = defaultdict(list)
    for (event, current), nxt in RUN_TRANSITIONS.items():
        if current is None:
            continue
        inverse[current].append((event, nxt))
    return dict(inverse)


_NEXT_STEPS: Final[Mapping[RunState, list[tuple[EventType, RunState]]]] = _build_next_steps()
