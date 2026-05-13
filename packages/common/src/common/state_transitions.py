"""Pure event → state transition logic.

The event_projector imports :func:`apply_run_transition` and applies the
result with a DDB ``ConditionExpression`` on the previous state, so the
same event re-delivered is a no-op (idempotency).

Two layers:

* **Single-source transitions** — most events advance state from one
  specific predecessor to one specific successor. These live in the
  :data:`RUN_TRANSITIONS` mapping.
* **Wildcard transitions** — ``RUN.FAILED`` and ``RUN.CANCEL_REQUESTED``
  may arrive in any non-terminal state. These are handled inline in
  :func:`apply_run_transition`.

Advisory events (``TEST_REPORT.READY``, ``CODE_CRITIQUE.READY``,
``EVAL.DRIFT_DETECTED``) intentionally have no entry — they update
side data on the run row but do not advance the state cursor.
"""

from __future__ import annotations

from collections.abc import Mapping

from common.events import EventType
from common.state import (
    TERMINAL_RUN_STATES,
    RunState,
)

RUN_TRANSITIONS: Mapping[tuple[EventType, RunState | None], RunState] = {
    ("REQUEST.RECEIVED", None): RunState.received,
    ("ISSUE.TRIAGED", RunState.triaging): RunState.triage_decided,
    ("DESIGN.READY", RunState.architect_running): RunState.designed,
    ("CRITIQUE.READY", RunState.critic_running): RunState.critiqued,
    ("IMPL_PR.OPENED", RunState.implementer_running): RunState.impl_pr_open,
    ("REVIEW.READY", RunState.validation_running): RunState.validation_complete,
    ("REVISION.READY", RunState.revising): RunState.validation_running,
    ("CHECKS.PASSED", RunState.validation_complete): RunState.awaiting_human_merge,
    ("CHECKS.PASSED", RunState.awaiting_checks): RunState.awaiting_human_merge,
    ("CHECKS.FAILED", RunState.validation_complete): RunState.revising,
    ("CHECKS.FAILED", RunState.awaiting_checks): RunState.revising,
    ("CHECKS.FAILED", RunState.awaiting_human_merge): RunState.revising,
    ("IMPL.ITERATION_REQUESTED", RunState.awaiting_checks): RunState.revising,
    ("IMPL.ITERATION_REQUESTED", RunState.awaiting_human_merge): RunState.revising,
    ("IMPL.ITERATION_REQUESTED", RunState.impl_pr_open): RunState.revising,
    ("IMPL.ITERATION_REQUESTED", RunState.validation_running): RunState.revising,
    ("IMPL.ITERATION_REQUESTED", RunState.validation_complete): RunState.revising,
    ("RUN.COMPLETED", RunState.awaiting_human_merge): RunState.done,
    ("RUN.COMPLETED", RunState.proposer_running): RunState.done,
}
"""Run-level state transitions keyed by (event_type, current_state)."""


def apply_run_transition(
    *,
    event_type: EventType,
    current_state: RunState | None,
) -> RunState | None:
    """Compute the next run state for ``event_type`` from ``current_state``.

    Returns ``None`` if the event is not a state-advancing event from
    that state — the projector should leave the cursor untouched
    (advisory events) or treat the message as already-applied.

    Wildcard transitions handled inline:

    * ``RUN.FAILED`` from any non-terminal state advances to ``failed``.
    * ``RUN.CANCEL_REQUESTED`` from any non-terminal state advances to
      ``cancelled``. Already-terminal runs are left alone — once a run
      ends, cancellation is meaningless.
    """
    if event_type == "RUN.FAILED":
        if current_state in TERMINAL_RUN_STATES:
            return None
        return RunState.failed
    if event_type == "RUN.CANCEL_REQUESTED":
        if current_state in TERMINAL_RUN_STATES:
            return None
        return RunState.cancelled
    return RUN_TRANSITIONS.get((event_type, current_state))
