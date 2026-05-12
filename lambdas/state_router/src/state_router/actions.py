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
    fire-and-forget (2s read timeout); the agent emits its completion
    event when done.

    When all four advance fields are ``None`` the invoker fires
    unconditionally. Used for advisors gated by an outer
    :class:`GuardedAdvance` — the gate is the race protection, so each
    individual advisor invoke doesn't need its own.
    """

    runtime_arn: str
    runtime_session_id: str
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

    ``advance_on_no_change_to`` lets a compound op (currently
    ``open_spec_pr``) advance to a different state when its result
    carries ``no_change: true`` — e.g., the architect re-produced a
    spec identical to what's already on ``main``, so the run should
    skip ``spec_pr_open`` and land on ``spec_approved`` directly.
    """

    op: str
    args: dict[str, Any]
    target_pk: str | None = None
    target_sk: str | None = None
    advance_from: str | None = None
    advance_to: str | None = None
    advance_on_no_change_to: str | None = None
    record_pr_url_attrs: tuple[str, ...] = ()
    function_name: str | None = None
    """Override the invoked Lambda; defaults to ``repo_helper_function_name()`` when ``None``."""


@dataclass(frozen=True, slots=True)
class InvokeLambda:
    """Synchronous Lambda invoke with a raw JSON payload.

    Used for Lambdas that expect a flat input shape (e.g., lint_gate)
    rather than the repo_helper op-dispatch envelope. The payload is
    serialised as-is and the response must carry ``ok: true`` for the
    state advance to proceed.
    """

    function_name: str
    args: dict[str, Any]
    target_pk: str | None = None
    target_sk: str | None = None
    advance_from: str | None = None
    advance_to: str | None = None


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
class SeedTasks:
    """Write one ``pk=RUN#{run_id}, sk=TASK#{task_id}, status=pending`` row per id.

    Emitted from ``handle_spec_approved`` once the architect has produced
    a spec and a human has merged the spec PR. The router walks the
    ``run.task_ids`` set (populated by the projector from the SPEC.READY
    event) and writes each TASK row with ``status=pending``, conditional
    on ``attribute_not_exists(pk)`` so a redelivered beacon doesn't
    clobber a row that already exists in a later state.
    """

    run_id: str
    task_ids: tuple[str, ...]
    project_slug: str
    spec_slug: str


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
class DedupedAdvisors:
    """Fire reviewer + tester invocations only on a fresh PR head SHA.

    The executor fetches the impl PR's current head SHA via
    ``repo_helper.get_pr_head_sha`` and compares to the run's
    ``last_advisor_sha``. On match, no advisor invocations fire — the
    surrounding ``GuardedAdvance`` still advances the task to
    ``pending_approval`` so the FSM progresses. On mismatch, the
    executor invokes each advisor and writes ``last_advisor_sha``.

    Wrapped inside :class:`GuardedAdvance.on_success` so the GuardedAdvance
    is the race guard: only one router wins the state advance, and only
    that router runs the dedupe + advisor dispatch.
    """

    repo: str
    pr_url: str
    advisors: tuple[InvokeAgent, ...]


@dataclass(frozen=True, slots=True)
class OpenImplPr:
    """Open the unified impl PR and backfill ``pr_url`` to STATE + all TASK rows.

    Fired once per run on the first beacon where any task has reached
    a state proving it merged into the impl branch (``pr_open`` /
    ``pending_approval`` / ``iterating`` / ``blocked`` / ``merged``).

    Idempotent: the underlying ``repo_helper.open_pr`` returns the
    existing open PR for ``(head, base)`` if one was already opened.
    The backfill is a per-row ``UpdateItem`` loop so a transient
    failure on one row doesn't block the others; the next beacon
    retries via the same path because ``run.pr_url`` is still empty
    until the STATE row's backfill succeeds.
    """

    repo: str
    head: str
    base: str
    title: str
    body: str
    run_id: str
    task_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompoundAction:
    """Run several actions in sequence.

    Used when a single state transition has multiple side effects —
    e.g., seed task rows then advance the run state.
    ``CompoundAction`` may itself appear in the tuple —
    :func:`~.handler.execute` flattens recursively.

    Sub-actions execute independently: if one's conditional advance
    fails, subsequent ones still run. For "gate, then run on success"
    semantics use :class:`GuardedAdvance` instead.
    """

    actions: tuple[Action, ...]


@dataclass(frozen=True, slots=True)
class GuardedAdvance:
    """Atomically advance state; if it succeeds, run ``on_success``.

    The advance is the race guard: when multiple routers process the
    same beacon (visibility timeout exceeded), only one of them passes
    the conditional update. The winner runs ``on_success``; losers
    no-op via :func:`~.dispatch.decide` on their next read.

    Used by ``dispatch_advisors`` so the reviewer + tester invokes fire
    exactly once per beacon, even under concurrent delivery. The
    advisors themselves don't change task state, so without this gate
    a no-op conditional update (``advance_from == advance_to``) would
    let every concurrent router fire each advisor.
    """

    target_pk: str
    target_sk: str
    advance_from: str
    advance_to: str
    on_success: tuple[Action, ...] = ()


type Action = (
    Noop
    | InvokeAgent
    | EmitEvent
    | InvokeRepoHelper
    | InvokeLambda
    | DedupedAdvisors
    | OpenImplPr
    | WriteSyntheticSpec
    | SeedTasks
    | AdvanceState
    | GuardedAdvance
    | CompoundAction
)
