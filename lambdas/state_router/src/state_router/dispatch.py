"""Top-level dispatch entry — :func:`decide` for the state router.

Run handlers live in :mod:`state_router.dispatch_run`. There is no
per-task dispatch any more (the platform consolidated to one issue →
one impl PR, so there are no TASK rows).

Adding a new state = adding one entry to :data:`RUN_DISPATCH` (in
:mod:`state_router.dispatch_run`) and one handler function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.state import TERMINAL_RUN_STATES
from state_router.actions import Action, Noop
from state_router.dispatch_run import RUN_DISPATCH, terminal

if TYPE_CHECKING:
    from state_router.model import Run


def decide(run: Run) -> Action:
    """Top-level dispatch: returns the next action for ``run``.

    Terminal runs return a Noop — the handler deletes the SQS beacon
    instead of executing. Unknown states are also Noop (defensive
    against a forgotten dispatch table entry).
    """
    if run.current_state is None:
        return Noop("current_state not yet set by projector")
    if run.current_state in TERMINAL_RUN_STATES:
        return terminal(run)
    handler = RUN_DISPATCH.get(run.current_state)
    if handler is None:
        return Noop(f"unknown run state: {run.current_state}")
    return handler(run)


__all__ = [
    "RUN_DISPATCH",
    "decide",
]
