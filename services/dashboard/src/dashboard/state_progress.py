"""Derive display state from the latest event in a run's history.

Pure functions, no I/O. Given the run's most recent event type (the
``status`` field on the SUMMARY row), produce:

* ``agent_label`` — what's currently happening, or who's currently
  on the hook (e.g. "Human reviewer" while we wait for a merge).
* ``stuck_threshold_seconds`` — UI surfaces an amber indicator when
  the run sits at the same status longer than this.
* ``is_terminal`` — true when the run has wrapped up.

The state machine is gone; ``status`` (the latest event type) is the
only display input the dashboard reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

TERMINAL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"RUN.COMPLETED", "RUN.FAILED", "RUN.CANCEL_REQUESTED"},
)
"""Event types that mark a run as finished."""

_AGENT_LABELS: Final[Mapping[str, str]] = {
    "REQUEST.RECEIVED": "Triage",
    "TRIAGE.DISPATCHED": "Triage",
    "ISSUE.TRIAGED": "Architect",
    "ARCHITECT.DISPATCHED": "Architect",
    "DESIGN.READY": "Implementer",
    "IMPLEMENTER.DISPATCHED": "Implementer",
    "CRITIQUE.READY": "Implementer",
    "IMPL_PR.OPENED": "Human reviewer",
    "REVISION.READY": "Human reviewer",
    "IMPL.ITERATION_REQUESTED": "Implementer (revision)",
    "CHECKS.FAILED": "Implementer (revision)",
    "CHECKS.PASSED": "Human reviewer",
    "VALIDATION.REQUESTED": "Reviewer + Tester + Code-Critic",
    "VALIDATORS.DISPATCHED": "Reviewer + Tester + Code-Critic",
    "REVIEW.READY": "Human reviewer",
    "TEST_REPORT.READY": "Human reviewer",
    "CODE_CRITIQUE.READY": "Human reviewer",
    "PROPOSER.DISPATCHED": "Proposer",
}

_HUMAN_LABELS: Final[frozenset[str]] = frozenset({"Human reviewer"})

_AGENT_RUNNING_THRESHOLD_S: Final[int] = 15 * 60
_HUMAN_WAIT_THRESHOLD_S: Final[int] = 30 * 60


def agent_label(status: str | None) -> str | None:
    """Display name of the party working from this status, or ``None``."""
    if status is None:
        return None
    return _AGENT_LABELS.get(status)


def is_terminal(status: str | None) -> bool:
    """``True`` when ``status`` is a terminal event type."""
    return status in TERMINAL_EVENT_TYPES if status else False


def is_active(status: str | None) -> bool:
    """``True`` when ``status`` maps to an active agent or wait party."""
    return agent_label(status) is not None


def stuck_threshold_seconds(status: str | None) -> int | None:
    """Seconds at the same status before the UI flags the run as stuck."""
    label = agent_label(status)
    if label is None:
        return None
    if label in _HUMAN_LABELS:
        return _HUMAN_WAIT_THRESHOLD_S
    return _AGENT_RUNNING_THRESHOLD_S


def progress_dict(
    status: str | None,
    *,
    updated_at: str | None,
) -> dict[str, object] | None:
    """Build a JSON-serialisable payload for the "currently running" panel.

    Returns ``None`` for terminal states or unknown statuses.
    """
    if is_terminal(status):
        return None
    label = agent_label(status)
    if label is None or status is None:
        return None
    return {
        "agent": label,
        "status": status,
        "since": updated_at,
        "stuck_threshold_seconds": stuck_threshold_seconds(status),
    }
