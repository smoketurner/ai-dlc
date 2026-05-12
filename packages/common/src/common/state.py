"""Run + task state machine for the SQS-beacon orchestration.

The platform projects every event off the platform bus through
:mod:`common.state_transitions` to advance two cursors:

* ``RunState`` — the run-level state machine. One row per run at
  ``pk=RUN#{run_id}, sk=STATE``.
* ``TaskState`` — the per-task cursor. One row per task at
  ``pk=RUN#{run_id}, sk=TASK#{task_id}``.

The state router (``lambdas/state_router``) reads these cursors but never
writes them — only the event_projector advances state, on receipt of
events from the platform bus. This guarantees a single source of truth
and a deterministic, replayable transition log.

The states are intentionally flat (no hierarchical nesting) so the
dispatch table in the router stays trivially readable. Adding a state =
adding one entry to this enum, one entry to the TRANSITIONS table, and
one entry to the dispatch table.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    """Run-level state cursor. Lives on the ``sk=STATE`` row."""

    received = "received"
    triaging = "triaging"
    triage_decided = "triage_decided"

    spec_pending = "spec_pending"
    architect_running = "architect_running"
    spec_drafted = "spec_drafted"
    critic_running = "critic_running"
    spec_critiqued = "spec_critiqued"
    spec_pr_open = "spec_pr_open"
    spec_approved = "spec_approved"

    tasks_in_progress = "tasks_in_progress"
    tasks_complete = "tasks_complete"
    lint_gate_running = "lint_gate_running"

    validation_running = "validation_running"
    validation_complete = "validation_complete"
    revising = "revising"
    awaiting_human_merge = "awaiting_human_merge"

    proposer_running = "proposer_running"

    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class TaskState(StrEnum):
    """Per-task state cursor. Lives on each ``sk=TASK#{task_id}`` row."""

    pending = "pending"
    implementer_running = "implementer_running"
    pr_open = "pr_open"
    reviewer_running = "reviewer_running"
    tester_running = "tester_running"
    iterating = "iterating"
    pending_approval = "pending_approval"
    blocked = "blocked"
    merged = "merged"
    closed = "closed"
    failed = "failed"


TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.done, RunState.failed, RunState.cancelled},
)
"""Run states that mean no further dispatch is possible.

The state router deletes the SQS beacon when it observes a run in one of
these states; the stuck-run detector skips them.
"""


TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.merged, TaskState.closed, TaskState.failed},
)
"""Task states that mean no further dispatch is possible for that task.

The router's ``tasks_in_progress`` handler walks all task rows and emits
``RUN.COMPLETED`` once every task is terminal.
"""
