"""Action executors — apply the side effects returned by :func:`state_router.decide.decide`.

Every executor takes the action and the run's event history. The
event log is the only source of truth; nothing here writes to
DynamoDB. State advances happen entirely through events landing on
the platform bus → projector.

Per :class:`~state_router.actions.InvokeAgent`:

1. Build the AgentCore payload from the event log.
2. Emit the per-agent ``*.DISPATCHED`` marker as idempotency proof
   *before* the AgentCore invoke. This way a beacon redelivered
   after the marker projects will hit :class:`~.actions.Noop` in
   :func:`~state_router.decide.decide` instead of double-invoking.
3. Call AgentCore. On synchronous failure, emit ``RUN.FAILED`` so
   the run terminates cleanly instead of wedging.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from common.event_emit import publish
from common.events import (
    ArchitectDispatched,
    EventEnvelope,
    EventType,
    ImplementerDispatched,
    Payload,
    ProposerDispatched,
    RunFailed,
    TriageDispatched,
    ValidatorsDispatched,
)
from common.identity import runtime_user_id
from common.ids import CorrelationId, EventId, RunId, new_event_id
from state_router.actions import Action, Compound, EmitEvent, InvokeAgent, Noop
from state_router.aws import dispatch_to_runtime
from state_router.config import runtime_arn
from state_router.extract import (
    EnvelopeLike,
)
from state_router.extract import (
    correlation_id as extract_correlation_id,
)
from state_router.extract import (
    pr_url as extract_pr_url,
)
from state_router.extract import (
    project_slug as extract_project_slug,
)
from state_router.extract import (
    requestor as extract_requestor,
)
from state_router.extract import (
    requestor_sub as extract_requestor_sub,
)
from state_router.extract import (
    run_id as extract_run_id,
)
from state_router.payload import (
    architect_payload,
    implementer_payload,
    proposer_payload,
    triage_payload,
    validator_payload,
)

logger = Logger(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")


VALIDATOR_RUNTIMES: tuple[tuple[str, bool], ...] = (
    ("reviewer", True),
    ("tester", False),
    ("code_critic", True),
)
"""(agent_runtime_name, include_issue_context). The Reviewer gets the
issue title/body so it can adversarially check the architect's
load-bearing assumptions against the original issue text. The
Code-Critic gets the same fields so it can grade the PR against the
user's original ask. The Tester reviews the diff against the plan and
doesn't currently need issue context."""


def execute(action: Action, events: Sequence[EnvelopeLike]) -> None:
    """Walk the action and apply its side effect."""
    if isinstance(action, Noop):
        logger.debug("noop", extra={"reason": action.reason})
        return
    if isinstance(action, Compound):
        for sub in action.actions:
            execute(sub, events)
        return
    if isinstance(action, EmitEvent):
        publish(action.envelope)
        return
    if isinstance(action, InvokeAgent):
        execute_invoke(action, events)
        return
    logger.warning("unknown action type", extra={"action": type(action).__name__})


def execute_invoke(action: InvokeAgent, events: Sequence[EnvelopeLike]) -> None:
    """Emit the dispatch marker, then invoke the agent runtime.

    For ``agent="validators"`` the marker is emitted once and three
    separate AgentCore runtimes are invoked in sequence. Per-validator
    failures are logged but do not terminate the run — the human-driven
    validation request can be retried.
    """
    run_id_str = extract_run_id(events)
    if not run_id_str:
        logger.warning("invoke skipped — no run_id in events")
        return
    publish_dispatch_marker(action, events)
    if action.agent == "validators":
        invoke_validators(action, events)
        return
    arn = runtime_arn(action.agent)
    if not arn:
        logger.warning("runtime ARN unset", extra={"agent": action.agent})
        emit_run_failed(events, reason=f"runtime ARN missing for {action.agent}")
        return
    payload = payload_for(action, events)
    if not payload:
        logger.warning("payload build returned empty — skipping", extra={"agent": action.agent})
        return
    success = dispatch_to_runtime(
        runtime_arn=arn,
        runtime_session_id=session_id_for(action, events),
        runtime_user_id=runtime_user_id_from(events),
        payload=payload,
    )
    if not success:
        emit_run_failed(events, reason=f"AgentCore invoke failed for {action.agent}")
        return
    metrics.add_metric(name="AgentDispatched", unit=MetricUnit.Count, value=1)


def invoke_validators(action: InvokeAgent, events: Sequence[EnvelopeLike]) -> None:
    """Fan out to reviewer + tester + code_critic in sequence."""
    for runtime_name, include_issue in VALIDATOR_RUNTIMES:
        arn = runtime_arn(runtime_name)
        if not arn:
            logger.warning("validator runtime ARN unset", extra={"agent": runtime_name})
            continue
        payload = validator_payload(
            events,
            revision_number=action.revision_number,
            include_issue_context=include_issue,
        )
        ok = dispatch_to_runtime(
            runtime_arn=arn,
            runtime_session_id=f"{extract_run_id(events)}-{runtime_name}-r{action.revision_number}",
            runtime_user_id=runtime_user_id_from(events),
            payload=payload,
        )
        if ok:
            metrics.add_metric(name="AgentDispatched", unit=MetricUnit.Count, value=1)
        else:
            logger.warning("validator dispatch failed", extra={"agent": runtime_name})


def session_id_for(action: InvokeAgent, events: Sequence[EnvelopeLike]) -> str:
    """Deterministic AgentCore session id for an agent dispatch."""
    run_id_str = extract_run_id(events)
    if action.agent == "implementer":
        if action.mode == "revision":
            return f"{run_id_str}-implementer-r{action.revision_number}"
        return f"{run_id_str}-implementer"
    return f"{run_id_str}-{action.agent}"


def runtime_user_id_from(events: Sequence[EnvelopeLike]) -> str:
    """Derive ``runtimeUserId`` for AgentCore identity scoping."""
    return runtime_user_id(
        requestor_sub=extract_requestor_sub(events),
        requestor=extract_requestor(events),
        fallback="system:state_router",
    )


def payload_for(
    action: InvokeAgent,
    events: Sequence[EnvelopeLike],
) -> dict[str, Any]:
    """Build the AgentCore payload for the requested agent."""
    if action.agent == "triage":
        return triage_payload(events)
    if action.agent == "architect":
        return architect_payload(events)
    if action.agent == "implementer":
        return implementer_payload(
            events,
            mode=action.mode,
            revision_number=action.revision_number,
        )
    if action.agent == "proposer":
        return proposer_payload(events)
    return {}


def publish_dispatch_marker(
    action: InvokeAgent,
    events: Sequence[EnvelopeLike],
) -> None:
    """Emit the ``*.DISPATCHED`` event matching the agent being invoked."""
    payload, event_type = dispatch_marker_payload(action, events)
    if payload is None:
        return
    envelope = build_envelope(event_type, payload, events)
    publish(envelope)


def dispatch_marker_payload(
    action: InvokeAgent,
    events: Sequence[EnvelopeLike],
) -> tuple[Payload | None, EventType]:
    """Build the right marker payload for the agent."""
    slug = extract_project_slug(events)
    session_id = session_id_for(action, events)
    if action.agent == "triage":
        return (TriageDispatched(project_slug=slug, session_id=session_id), "TRIAGE.DISPATCHED")
    if action.agent == "architect":
        return (
            ArchitectDispatched(project_slug=slug, session_id=session_id),
            "ARCHITECT.DISPATCHED",
        )
    if action.agent == "implementer":
        mode = "revision" if action.mode == "revision" else "implementation"
        return (
            ImplementerDispatched(
                project_slug=slug,
                session_id=session_id,
                mode=mode,  # type: ignore[arg-type]
                revision_number=action.revision_number,
            ),
            "IMPLEMENTER.DISPATCHED",
        )
    if action.agent == "validators":
        return (
            ValidatorsDispatched(
                project_slug=slug,
                pr_url=extract_pr_url(events),
                revision_number=action.revision_number,
            ),
            "VALIDATORS.DISPATCHED",
        )
    if action.agent == "proposer":
        return (
            ProposerDispatched(project_slug=slug, session_id=session_id),
            "PROPOSER.DISPATCHED",
        )
    return (None, "RUN.FAILED")  # unreachable


def build_envelope(
    event_type: EventType,
    payload: Payload,
    events: Sequence[EnvelopeLike],
) -> EventEnvelope[Any]:
    """Wrap a payload in an envelope that inherits the run's ids + correlation."""
    return EventEnvelope[Any](  # type: ignore[type-abstract]
        event_id=EventId(new_event_id()),
        type=event_type,
        run_id=RunId(extract_run_id(events)),
        correlation_id=CorrelationId(extract_correlation_id(events)),
        actor_id="state_router",
        payload=payload,
    )


def emit_run_failed(events: Sequence[EnvelopeLike], *, reason: str) -> None:
    """Emit ``RUN.FAILED`` with the failure reason from the executor."""
    payload = RunFailed(
        project_slug=extract_project_slug(events),
        failed_state="dispatch_failure",
        error_class="DispatchFailed",
        error_message=reason[:512],
        retryable=False,
    )
    envelope = build_envelope("RUN.FAILED", payload, events)
    publish(envelope)
    metrics.add_metric(name="DispatchFailure", unit=MetricUnit.Count, value=1)
    logger.warning("emitted RUN.FAILED", extra={"reason": reason})
