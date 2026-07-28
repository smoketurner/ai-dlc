"""Pure decision function — events → next router action.

The handler queries every ``EVENT#*`` row for one run and passes the
parsed envelope list to :func:`decide`. The function returns an
:data:`~state_router.actions.Action` describing what the router should
do next. Idempotency is structural: each agent dispatch leaves a
``*.DISPATCHED`` marker event behind, and :func:`decide` returns
:class:`~state_router.actions.Noop` whenever the marker for the action
it would otherwise emit is already present in the run's history.

Properties — verified by unit tests:

* **Pure**. No IO. Same input → same output.
* **Replay-safe**. Replaying any suffix of the run's events yields
  ``Noop`` once the run has caught up to its current dispatched state.
* **Order-aware**. Decisions look at the *latest* event for human-driven
  branches (``IMPL.ITERATION_REQUESTED``, ``CHECKS.FAILED``,
  ``VALIDATION.REQUESTED``); the linear pre-PR pipeline branches on the
  *set* of event types seen. The reviewer's verdict is the exception —
  the three validators finish in any order, so it is looked up within
  the current validation pass rather than at the tail.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from common.events import (
    EventEnvelope,
    EventType,
    RunCancelRequested,
    RunFailed,
)
from common.ids import CorrelationId, RunId, new_event_id
from state_router.actions import (
    Action,
    AgentKind,
    Compound,
    EmitEvent,
    InvokeAgent,
    Noop,
)
from state_router.extract import EnvelopeLike, get, pr_url, source_issue_url

TERMINAL_EVENTS: frozenset[EventType] = frozenset(
    {"RUN.COMPLETED", "RUN.FAILED", "RUN.CANCEL_REQUESTED"},
)
"""Event types that mean the run is finished — :func:`decide` always Noops."""

DISPATCH_MARKERS: Mapping[AgentKind, EventType] = {
    "triage": "TRIAGE.DISPATCHED",
    "architect": "ARCHITECT.DISPATCHED",
    "implementer": "IMPLEMENTER.DISPATCHED",
    "validators": "VALIDATORS.DISPATCHED",
    "proposer": "PROPOSER.DISPATCHED",
}
"""Per-agent idempotency-proof event type.

The executor emits the marker after a successful AgentCore invoke. The
decision function uses ``marker is in events`` as "agent X is already
running (or has run) for this trigger — don't dispatch again."
"""

# Triage outcomes that terminate the run with a cancel.
CANCEL_TRIAGE_ACTIONS: frozenset[str] = frozenset({"ask", "defer", "decline"})

MAX_REVISIONS = 3
"""Ceiling on *automated* revision passes (reviewer verdict + CI failure).

Past this the loop has stopped converging and burning more tokens is
waste — the run fails with ``error_class="RevisionCapReached"`` so a
human picks it up. Revisions a human asked for via an ``@aidlc-bot``
mention are deliberately uncapped: someone is already steering.
"""


def decide(events: Sequence[EnvelopeLike]) -> Action:
    """Compute the router's next action for one run.

    See module docstring for the contract.
    """
    if not events:
        return Noop("no events")

    type_set: set[EventType] = {e.type for e in events}

    if type_set & TERMINAL_EVENTS:
        return Noop("terminal")

    request = first_of(events, "REQUEST.RECEIVED")
    if request is None:
        return Noop("waiting for REQUEST.RECEIVED")

    return decide_after_request(events, type_set, request)


def decide_after_request(
    events: Sequence[EnvelopeLike],
    type_set: set[EventType],
    request: EnvelopeLike,
) -> Action:
    """Branch the decision tree once we have a ``REQUEST.RECEIVED`` event."""
    # Issue-driven runs go through triage first. Programmatic runs skip
    # straight to the architect. The payload carries this discriminator.
    if has_source_issue(request) and "ISSUE.TRIAGED" not in type_set:
        return invoke_unless_dispatched(events, "triage")

    triage = first_of(events, "ISSUE.TRIAGED")
    if triage is None and "ISSUE.TRIAGED" not in type_set:
        # Programmatic run: no triage; jump to architect.
        if "DESIGN.READY" not in type_set:
            return invoke_unless_dispatched(events, "architect")
        return decide_after_design(events, type_set)

    if triage is None:
        return Noop("waiting for ISSUE.TRIAGED projection")

    return decide_after_triage(events, type_set, triage)


def decide_after_triage(
    events: Sequence[EnvelopeLike],
    type_set: set[EventType],
    triage: EnvelopeLike,
) -> Action:
    """Branch on the triage agent's ``action``."""
    action: str = get(triage, "action", "")
    if action in CANCEL_TRIAGE_ACTIONS:
        return emit_cancel(events, reason=f"triage:{action}")
    if action == "research":
        return invoke_unless_dispatched(events, "proposer")
    if action == "proceed":
        if "DESIGN.READY" not in type_set:
            return invoke_unless_dispatched(events, "architect")
        return decide_after_design(events, type_set)
    return Noop(f"unknown triage action: {action}")


def decide_after_design(
    events: Sequence[EnvelopeLike],
    type_set: set[EventType],
) -> Action:
    """Once the plan is ready, drive the implementer and then react to PR signals."""
    if "IMPL_PR.OPENED" not in type_set:
        return invoke_unless_dispatched(events, "implementer", mode="implementation")
    return decide_after_pr_open(events)


def decide_after_pr_open(events: Sequence[EnvelopeLike]) -> Action:
    """React to the latest post-PR signal.

    Auto-dispatches validators when the impl PR first opens or after the
    implementer pushes a revision. Auto-dispatches the implementer in
    revision mode when CI fails, when the reviewer returns
    ``request_changes``, or when a human ``@aidlc-bot`` mention asks for
    changes. ``VALIDATION.REQUESTED`` is the manual nudge path — same
    target as the IMPL_PR.OPENED / REVISION.READY branch.
    """
    last = events[-1]
    if last.type in ("IMPL.ITERATION_REQUESTED", "CHECKS.FAILED"):
        # Human mentions steer the run directly and stay uncapped; CI
        # failures are automated and count toward MAX_REVISIONS.
        return revise(events, last, capped=last.type == "CHECKS.FAILED")
    if last.type in ("IMPL_PR.OPENED", "REVISION.READY", "VALIDATION.REQUESTED"):
        return invoke_unless_dispatched(
            events,
            "validators",
            revision_number=current_revision_number(events),
            trigger_event_id=last.event_id,
        )
    blocking_review = pending_blocking_review(events)
    if blocking_review is not None:
        return revise(events, blocking_review, capped=True)
    return Noop(f"waiting after {last.type}")


def pending_blocking_review(events: Sequence[EnvelopeLike]) -> EnvelopeLike | None:
    """Return the ``REVIEW.READY`` that should trigger a revision, if any.

    The three validators run concurrently, so ``REVIEW.READY`` is rarely
    the tail event — the code-critic or tester usually lands after it.
    Scope the lookup to the current validation pass (everything after the
    most recent ``VALIDATORS.DISPATCHED`` marker) instead of inspecting
    only ``events[-1]``, so a ``request_changes`` verdict is still seen
    when another validator reports last.
    """
    for event in reversed(events_since_last_validation(events)):
        if event.type == "REVIEW.READY":
            verdict = get(event, "verdict", "")
            return event if verdict == "request_changes" else None
    return None


def events_since_last_validation(events: Sequence[EnvelopeLike]) -> Sequence[EnvelopeLike]:
    """Slice the history down to the current validation pass."""
    for index in range(len(events) - 1, -1, -1):
        if events[index].type == "VALIDATORS.DISPATCHED":
            return events[index + 1 :]
    return events


def revise(
    events: Sequence[EnvelopeLike],
    trigger: EnvelopeLike,
    *,
    capped: bool,
) -> Action:
    """Dispatch an implementer revision, honouring the automated-revision cap."""
    revision_number = current_revision_number(events) + 1
    if capped and revision_number > MAX_REVISIONS:
        return emit_revision_cap_reached(events)
    return invoke_unless_dispatched(
        events,
        "implementer",
        mode="revision",
        revision_number=revision_number,
        trigger_event_id=trigger.event_id,
    )


def emit_revision_cap_reached(events: Sequence[EnvelopeLike]) -> Action:
    """Terminate the run once the automated revision loop stops converging."""
    # No "already emitted" guard needed — RUN.FAILED is in TERMINAL_EVENTS,
    # so decide() short-circuits to Noop before reaching this branch again.
    request = first_of(events, "REQUEST.RECEIVED")
    if request is None:
        return Noop("cannot fail without REQUEST.RECEIVED")
    revision_count = current_revision_number(events)
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(str(request.run_id)),
        correlation_id=CorrelationId(str(request.correlation_id)),
        actor_id="state_router",
        payload=RunFailed(
            project_slug=get(request, "project_slug", ""),
            failed_state="revising",
            error_class="RevisionCapReached",
            error_message=(
                f"automated revision loop hit the cap of {MAX_REVISIONS} "
                f"after {revision_count} revisions — a human needs to take over"
            ),
            retryable=False,
            pr_url=pr_url(events),
            source_issue_url=source_issue_url(events) or "",
            revision_count=revision_count,
        ),
    )
    return Compound((EmitEvent(envelope=envelope),))


def invoke_unless_dispatched(
    events: Sequence[EnvelopeLike],
    agent: AgentKind,
    *,
    mode: str = "implementation",
    revision_number: int = 0,
    trigger_event_id: str | None = None,
) -> Action:
    """Return :class:`InvokeAgent` unless the dispatch marker is already present.

    For pre-PR steps the marker absence anywhere in the event log is
    sufficient (each agent dispatches at most once before the PR). For
    post-PR steps the marker must be later than the triggering event —
    successive ``IMPL.ITERATION_REQUESTED`` events each need their own
    implementer dispatch.
    """
    marker = DISPATCH_MARKERS[agent]
    if trigger_event_id is None:
        # Pre-PR: agent runs once per run. Marker presence anywhere = done.
        if any(e.type == marker for e in events):
            return Noop(f"{agent} already dispatched")
    elif has_event_after(events, marker, after_event_id=trigger_event_id):
        return Noop(f"{agent} already dispatched for trigger {trigger_event_id}")
    if agent == "implementer":
        return InvokeAgent(
            agent=agent,
            mode="revision" if mode == "revision" else "implementation",
            revision_number=revision_number,
        )
    if agent == "validators":
        return InvokeAgent(agent=agent, revision_number=revision_number)
    return InvokeAgent(agent=agent)


def emit_cancel(events: Sequence[EnvelopeLike], *, reason: str) -> Action:
    """Build a ``RUN.CANCEL_REQUESTED`` envelope from triage outcome."""
    request = first_of(events, "REQUEST.RECEIVED")
    if request is None:
        return Noop("cannot cancel without REQUEST.RECEIVED")
    if any(e.type == "RUN.CANCEL_REQUESTED" for e in events):
        return Noop("cancel already emitted")
    envelope = EventEnvelope[RunCancelRequested](
        event_id=new_event_id(),
        type="RUN.CANCEL_REQUESTED",
        run_id=RunId(str(request.run_id)),
        correlation_id=CorrelationId(str(request.correlation_id)),
        actor_id="state_router",
        payload=RunCancelRequested(
            project_slug=getattr(request.payload, "project_slug", ""),
            requestor=getattr(request.payload, "requestor", "system"),
            source="comment_command",
            reason=reason,
        ),
    )
    return Compound((EmitEvent(envelope=envelope),))


def first_of(
    events: Sequence[EnvelopeLike],
    event_type: EventType,
) -> EnvelopeLike | None:
    """Return the first event whose type matches, or ``None``."""
    for event in events:
        if event.type == event_type:
            return event
    return None


def has_event_after(
    events: Sequence[EnvelopeLike],
    event_type: EventType,
    *,
    after_event_id: str,
) -> bool:
    """``True`` iff ``event_type`` appears strictly after ``after_event_id``."""
    seen_after = False
    for event in events:
        if seen_after and event.type == event_type:
            return True
        if str(event.event_id) == after_event_id:
            seen_after = True
    return False


def has_source_issue(request: EnvelopeLike) -> bool:
    """``True`` iff the run was triggered by a GitHub issue (vs programmatic)."""
    return bool(get(request, "source_issue_url"))


def current_revision_number(events: Sequence[EnvelopeLike]) -> int:
    """Count of completed revision passes for this run."""
    return sum(1 for e in events if e.type == "REVISION.READY")
