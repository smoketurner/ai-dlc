"""Action types returned by dispatch handlers.

Each dispatch handler is a pure function from :class:`~.model.Run` to
:data:`Action` — no side effects, no DDB or network. The handler in
:mod:`.handler` then walks the action and applies it via the
``Services`` bundle.

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
    """The router is waiting on an external event; do nothing.

    The SQS visibility timeout will expire and the beacon will be
    re-delivered, at which point the next event projection may have
    advanced state and the router will pick up the new action.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class InvokeAgent:
    """Conditionally advance state to ``advance_to``, then fire the agent.

    The advance is a DDB ``UpdateItem`` with a ``ConditionExpression``
    on the previous state — only one router instance wins the race; the
    loser sees the new state on the next poll and no-ops. Agent invoke
    is fire-and-forget (2s read timeout). The agent emits its
    completion event when done.

    ``target_pk`` / ``target_sk`` identify whether we're advancing the
    run's STATE row or one of its TASK rows; ``advance_from`` is the
    expected current value for the conditional update.
    """

    runtime_arn: str
    runtime_session_id: str
    payload: dict[str, Any]
    target_pk: str
    target_sk: str
    advance_from: str
    advance_to: str


@dataclass(frozen=True, slots=True)
class EmitEvent:
    """Emit one envelope onto the platform EventBridge bus.

    The payload type is intentionally left as ``Any`` so any specialised
    :class:`EventEnvelope` (``EventEnvelope[RunCompleted]``,
    ``EventEnvelope[TaskApproved]``, …) is assignable. The ``publish``
    helper picks the right serialisation path by reading the envelope's
    own ``type``.
    """

    envelope: EventEnvelope[Any]


@dataclass(frozen=True, slots=True)
class InvokeRepoHelper:
    """Synchronous Lambda invoke of ``repo_helper`` (open PR, comment, etc.).

    Used for control-plane GitHub ops the router can't fire-and-forget:
    opening the spec PR (we need the PR URL written back to DDB),
    posting a "starting iteration N" comment, etc.

    ``advance_*`` fields are populated when the router should also
    advance state on success (e.g., ``spec_critiqued → spec_pr_open``
    after the PR is open). When ``advance_to`` is ``None``, the call is
    informational and state stays put.
    """

    op: str
    args: dict[str, Any]
    target_pk: str | None = None
    target_sk: str | None = None
    advance_from: str | None = None
    advance_to: str | None = None
    record_pr_url_attr: str | None = None


@dataclass(frozen=True, slots=True)
class WriteSyntheticSpec:
    """Upload a 1-task synthetic spec bundle for non-``spec_driven`` workflows.

    Triage classifies ``bug_fix`` / ``upgrade`` / ``docs`` workflows;
    the router writes the synthetic spec to S3 inline before
    transitioning the run to ``tasks_in_progress`` so the implementer
    has something to read.

    The actual spec content is rendered in the dispatch handler from
    the run's intent + the triage decision. The S3 upload happens in
    :mod:`.handler`.
    """

    s3_key_prefix: str
    requirements_md: str
    design_md: str
    tasks_md: str
    target_pk: str
    target_sk: str
    advance_from: str
    advance_to: str


@dataclass(frozen=True, slots=True)
class AdvanceState:
    """Conditionally advance state with no other side effect.

    Used for purely-bookkeeping transitions (e.g., a run with no
    source-issue and a synthetic spec advances directly to
    ``tasks_in_progress`` without a triage step).
    """

    target_pk: str
    target_sk: str
    advance_from: str
    advance_to: str


@dataclass(frozen=True, slots=True)
class CompoundAction:
    """Run several actions in sequence.

    Used when a single state transition has multiple side effects —
    e.g., dispatch advisors (reviewer + tester) is two ``InvokeAgent``
    actions plus one ``AdvanceState``. ``CompoundAction`` may itself
    appear in the tuple — :func:`~.handler.execute` flattens recursively.
    """

    actions: tuple[Action, ...]


type Action = (
    Noop
    | InvokeAgent
    | EmitEvent
    | InvokeRepoHelper
    | WriteSyntheticSpec
    | AdvanceState
    | CompoundAction
)
