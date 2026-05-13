"""Action types returned by dispatch handlers.

Each dispatch handler is a pure function from :class:`~.model.Run` to
:data:`Action` — no side effects, no DDB or network. The handler in
:mod:`.handler` then walks the action and applies it via the executor
in :mod:`.execute`.

Splitting "decide" from "execute" makes the dispatch table trivially
testable: a unit test asserts that a given run state produces the
expected ``InvokeAgent(...)`` action without needing AWS clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.events import EventEnvelope


@dataclass(frozen=True, slots=True)
class Noop:
    """The router has no action to take for this beacon; ack and stop.

    Emitted for wait states (run parked on an external event — human PR
    review, async agent response, awaiting webhook) and for terminal
    states. :func:`~.execute.execute_noop` logs the reason and returns;
    :func:`~.handler.lambda_handler` then acks the SQS message.

    The router wakes again only when the projector writes a fresh OUTBOX
    row in response to a new event landing on the platform bus — never
    from this beacon being re-delivered. Visibility-timeout redelivery
    happens only on uncaught Lambda exceptions, which is pure error
    retry, not a state-machine tick.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class InvokeAgent:
    """Optionally advance state, then fire the agent.

    When ``advance_from`` / ``advance_to`` are set, the invoker does a
    DDB ``UpdateItem`` with a ``ConditionExpression`` on the previous
    state — only one router instance wins the race; the loser sees the
    new state on the next poll and no-ops. Agent invoke is
    fire-and-forget (the runtime returns ~immediately under the
    async-task pattern); the agent emits its completion event when
    done.

    When all four advance fields are ``None`` the invoker fires
    unconditionally. Used for validators that fire in parallel from a
    single state advance — the surrounding AdvanceState (or another
    invoke in the same CompoundAction) owns the race guard.
    """

    runtime_arn: str
    runtime_session_id: str
    runtime_user_id: str
    payload: dict[str, Any]
    target_pk: str | None = None
    target_sk: str | None = None
    advance_from: str | None = None
    advance_to: str | None = None


@dataclass(frozen=True, slots=True)
class EmitEvent:
    """Emit one envelope onto the platform EventBridge bus.

    The payload type is intentionally left as ``Any`` so any specialised
    :class:`EventEnvelope` (``EventEnvelope[RunCompleted]``,
    ``EventEnvelope[ChecksPassed]``, …) is assignable. The ``publish``
    helper picks the right serialisation path by reading the envelope's
    own ``type``.
    """

    envelope: EventEnvelope[Any]


@dataclass(frozen=True, slots=True)
class InvokeRepoHelper:
    """Synchronous Lambda invoke of ``repo_helper`` (comment PR, label issue, etc.).

    Used for control-plane GitHub ops the router can't fire-and-forget.
    ``advance_*`` fields are populated when the router should also
    advance state on success. When ``advance_to`` is ``None``, the call
    is informational (``comment_issue`` / ``label_issue``) and state
    stays put.

    ``advance_on_no_change_to`` is a no-op in the single-PR-per-issue
    world (no ``open_spec_pr`` short-circuit any more) but is kept on
    the dataclass for forward-compat with any future op that wants the
    same pattern.
    """

    op: str
    args: dict[str, Any]
    target_pk: str | None = None
    target_sk: str | None = None
    advance_from: str | None = None
    advance_to: str | None = None
    advance_on_no_change_to: str | None = None
    record_pr_url_attrs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdvanceState:
    """Conditionally advance state with no other side effect.

    Used for purely-bookkeeping transitions (e.g., advance to
    ``validation_running`` after dispatching the three validators in
    parallel).
    """

    target_pk: str
    target_sk: str
    advance_from: str
    advance_to: str


@dataclass(frozen=True, slots=True)
class CompoundAction:
    """Run several actions in sequence.

    Used when a single state transition has multiple side effects —
    e.g., dispatch three validators then advance the run state.
    ``CompoundAction`` may itself appear in the tuple —
    :func:`~.handler.execute` flattens recursively.

    Sub-actions execute independently: if one's conditional advance
    fails, subsequent ones still run.
    """

    actions: tuple[Action, ...]


type Action = Noop | InvokeAgent | EmitEvent | InvokeRepoHelper | AdvanceState | CompoundAction


def impl_branch_name(run_id: str) -> str:
    """Conventional impl branch name.

    Mirrors ``implementer.repo_ops.impl_branch_name``. There is one
    impl branch per run (the implementer opens a single PR off this
    branch and applies all revisions to it directly).
    """
    return f"aidlc/impl/{run_id}"
