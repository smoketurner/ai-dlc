"""Action types returned by :func:`state_router.decide.decide`.

Pure dataclasses; no side effects. The handler walks the returned
action and applies it via the executor in :mod:`state_router.execute`.

Splitting decide from execute keeps the decision logic trivially
unit-testable — assert that a given event history produces the
expected ``InvokeAgent(...)`` without needing AWS clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from common.events import EventEnvelope

AgentKind = Literal[
    "triage",
    "architect",
    "implementer",
    "validators",
    "proposer",
]
"""Logical agent identifiers used by :class:`InvokeAgent`.

``validators`` is a fan-out invoking reviewer + tester + code_critic in
parallel — the executor handles the fan-out so :func:`decide` can stay
simple.
"""


@dataclass(frozen=True, slots=True)
class Noop:
    """No action — beacon will be ack'd.

    Emitted when the run is waiting on an external event (human PR
    review, async agent response, in-flight dispatch) or has reached a
    terminal state. The next beacon arrives only when a new event is
    projected — not from re-delivery of this one.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class InvokeAgent:
    """Invoke an AgentCore runtime.

    The executor builds the AgentCore payload from the run's event
    history, emits the matching ``*.DISPATCHED`` marker as idempotency
    proof, and fires the invoke.

    For ``agent="implementer"``, ``mode`` selects between an initial
    implementation pass and a revision pass; ``revision_number`` is the
    0-based ordinal of this dispatch (0 for the initial implementation,
    1+ for revisions).

    For ``agent="validators"``, ``revision_number`` is the revision
    ordinal that the validators are evaluating (0 for the initial PR,
    1+ for subsequent revisions).
    """

    agent: AgentKind
    mode: Literal["implementation", "revision"] = "implementation"
    revision_number: int = 0


@dataclass(frozen=True, slots=True)
class EmitEvent:
    """Emit one envelope onto the platform EventBridge bus.

    Used for router-emitted events: dispatch markers, terminal
    transitions when a triage outcome should end the run, etc.
    """

    envelope: EventEnvelope[Any]


@dataclass(frozen=True, slots=True)
class Compound:
    """Execute several actions in sequence.

    Used when a single decision produces multiple side effects — e.g.
    cancel + emit a comment, or emit a dispatch marker + invoke. The
    executor walks the tuple in order; nested ``Compound`` is flattened.
    """

    actions: tuple[Action, ...]


type Action = Noop | InvokeAgent | EmitEvent | Compound


def impl_branch_name(run_id: str) -> str:
    """Conventional impl branch name (one per run).

    Mirrors :func:`implementer.repo_ops.impl_branch_name`. The
    implementer opens a single PR off this branch and applies all
    revisions to it directly.
    """
    return f"aidlc/impl/{run_id}"
