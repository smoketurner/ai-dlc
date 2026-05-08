"""Pure event → state transition logic.

The event_projector imports :func:`apply_run_transition` /
:func:`apply_task_transition` and applies the result with a DDB
``ConditionExpression`` on the previous state, so the same event re-
delivered is a no-op (idempotency).

Two layers:

* **Single-source transitions** — most events advance state from one
  specific predecessor to one specific successor. These live in the
  :data:`RUN_TRANSITIONS` and :data:`TASK_TRANSITIONS` mappings.
* **Wildcard transitions** — ``RUN.FAILED`` and ``RUN.CANCEL_REQUESTED``
  may arrive in any non-terminal state. These are handled inline in
  :func:`apply_run_transition`.

Advisory events (``REVIEW.READY``, ``TEST_REPORT.READY``,
``CRITIQUE.READY`` for the spec gate, ``ISSUE.ASK_POSTED``,
``EVAL.DRIFT_DETECTED``) intentionally have no entry — they update
side data on the run/task row but do not advance the state cursor.
``CRITIQUE.READY`` IS a state-advancing event for the run-level
state machine (architect → critic gate); only the spec-quality
metrics it carries are advisory.
"""

from __future__ import annotations

from collections.abc import Mapping

from common.events import EventType
from common.state import (
    TERMINAL_RUN_STATES,
    TERMINAL_TASK_STATES,
    RunState,
    TaskState,
)

RUN_TRANSITIONS: Mapping[tuple[EventType, RunState | None], RunState] = {
    ("REQUEST.RECEIVED", None): RunState.received,
    ("ISSUE.TRIAGED", RunState.triaging): RunState.triage_decided,
    ("SPEC.READY", RunState.architect_running): RunState.spec_drafted,
    ("CRITIQUE.READY", RunState.critic_running): RunState.spec_critiqued,
    ("SPEC.APPROVED", RunState.spec_pr_open): RunState.spec_approved,
    ("SPEC.REJECTED", RunState.spec_pr_open): RunState.failed,
    ("RUN.COMPLETED", RunState.tasks_complete): RunState.done,
    ("RUN.COMPLETED", RunState.proposer_running): RunState.done,
}
"""Run-level state transitions keyed by (event_type, current_state)."""


TASK_TRANSITIONS: Mapping[tuple[EventType, TaskState], TaskState] = {
    ("TASK.READY", TaskState.implementer_running): TaskState.pr_open,
    ("TASK.READY", TaskState.iterating): TaskState.pr_open,
    ("TASK.BLOCKED", TaskState.implementer_running): TaskState.blocked,
    ("TASK.BLOCKED", TaskState.iterating): TaskState.blocked,
    ("TASK.ITERATION_REQUESTED", TaskState.pr_open): TaskState.iterating,
    ("TASK.ITERATION_REQUESTED", TaskState.pending_approval): TaskState.iterating,
    ("TASK.ITERATION_REQUESTED", TaskState.blocked): TaskState.iterating,
    ("TASK.APPROVED", TaskState.pending_approval): TaskState.merged,
    ("TASK.APPROVED", TaskState.blocked): TaskState.merged,
    ("TASK.REJECTED", TaskState.pending_approval): TaskState.closed,
    ("TASK.REJECTED", TaskState.blocked): TaskState.closed,
}
"""Task-level state transitions keyed by (event_type, current_state)."""


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


def apply_task_transition(
    *,
    event_type: EventType,
    current_state: TaskState,
) -> TaskState | None:
    """Compute the next task state for ``event_type`` from ``current_state``.

    Returns ``None`` if the event does not advance task state from that
    cursor — most commonly because the event is advisory
    (``REVIEW.READY``, ``TEST_REPORT.READY``) or already-applied
    (re-delivery while task is in ``pr_open`` and the same TASK.READY
    event arrives twice, etc.).

    Wildcard handling: ``TASK.REJECTED`` and ``TASK.APPROVED`` are
    only valid from ``pending_approval``. A task closed via PR-close
    webhook on a non-pending state (e.g., user closes a PR while
    advisors are still running) is handled by the dashboard webhook
    emitting the right event after a state-aware lookup.
    """
    if current_state in TERMINAL_TASK_STATES:
        return None
    return TASK_TRANSITIONS.get((event_type, current_state))
