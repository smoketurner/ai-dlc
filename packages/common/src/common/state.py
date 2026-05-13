"""Run state machine for the SQS-beacon orchestration.

The platform projects every event off the platform bus through
:mod:`common.state_transitions` to advance a single cursor:

* ``RunState`` — the run-level state machine. One row per run at
  ``pk=RUN#{run_id}, sk=STATE``.

The state router (``lambdas/state_router``) reads this cursor but never
writes it — only the event_projector advances state, on receipt of
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
    """Run-level state cursor. Lives on the ``sk=STATE`` row.

    One issue → one PR. The flow is:

    ``received → triaging → triage_decided → architect_running → designed
    → critic_running → critiqued → implementer_running → impl_pr_open
    → validation_running → validation_complete →
      (awaiting_checks → awaiting_human_merge → done) | revising → ...``
    """

    received = "received"
    triaging = "triaging"
    triage_decided = "triage_decided"

    architect_running = "architect_running"
    designed = "designed"
    critic_running = "critic_running"
    critiqued = "critiqued"

    implementer_running = "implementer_running"
    impl_pr_open = "impl_pr_open"

    validation_running = "validation_running"
    validation_complete = "validation_complete"
    revising = "revising"
    awaiting_checks = "awaiting_checks"
    awaiting_human_merge = "awaiting_human_merge"

    proposer_running = "proposer_running"

    done = "done"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.done, RunState.failed, RunState.cancelled},
)
"""Run states that mean no further dispatch is possible.

The state router deletes the SQS beacon when it observes a run in one of
these states; the stuck-run detector skips them.
"""
