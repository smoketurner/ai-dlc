"""Projector Lambda — fans out platform events into the read model + memory.

Triggered by every event on the platform EventBridge bus. Every event
produces exactly one ``TransactWriteItems`` containing:

* The EVENT timeline row (``sk=EVENT#{event_id}``) with
  ``attribute_not_exists(sk)`` — this row is the master idempotency
  key. A re-delivered envelope fails the condition and the entire
  transaction rolls back.
* The STATE row Update — always carries the metadata clauses (status,
  ``updated_at``, ``if_not_exists(project_slug)``, GSI keys for
  issue-driven runs, usage ``ADD`` clauses) and, when the event
  advances run-level state or accumulates spec-iteration feedback,
  the corresponding clauses in the same Update.
* The TASK row Update — only when the event is task-level. Carries
  the task state advance or the in-place iteration accumulator.
* The OUTBOX row Put — only when state advanced. The EventBridge Pipe
  forwards it to the state-router beacon queue.

Because the transaction is atomic, re-delivery is a complete no-op —
no double-counted usage totals, no duplicate memory writes, no
duplicate outbox rows. Race losses (two events targeting the same row
concurrently) drop cleanly via the conditional-check on the STATE/TASK
update.

After a successful commit, the projector forwards the envelope to
AgentCore Memory via ``CreateEvent``. Memory writes are gated on the
transaction succeeding, so they're also idempotent on event_id.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError

from common.ddb import (
    PutBuilder,
    TransactWriteItemsBuilder,
    UpdateBuilder,
    deserialize_item,
)
from common.events import EventType as PlatformEventType
from common.events import UntypedEnvelope
from common.state import TERMINAL_RUN_STATES, RunState, TaskState
from common.state_transitions import (
    SPEC_ITERATION_ACCUMULATOR_STATES,
    apply_run_transition,
    apply_task_transition,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="event_projector")
tracer = Tracer(service="event_projector")
metrics = Metrics(namespace="ai-dlc", service="event_projector")

OUTBOX_TTL_SECONDS = 3600
"""How long an outbox row lives before DDB TTL sweeps it.

The pipe forwards within seconds; the stream record persists for 24h
regardless of TTL, so a pipe outage of up to 24h still recovers. TTL
is purely table hygiene.
"""

TASK_LEVEL_EVENTS = frozenset(
    {
        "TASK.READY",
        "TASK.BLOCKED",
        "TASK.APPROVED",
        "TASK.REJECTED",
        "TASK.ITERATION_REQUESTED",
    },
)
"""Events whose state cursor lives on a TASK row, not the STATE row."""

ITERATION_ACCUMULATOR_STATES = frozenset(
    {TaskState.iterating, TaskState.implementer_running},
)
"""Task states where ``TASK.ITERATION_REQUESTED`` queues feedback in place.

A second ``/aidlc fix X`` arriving while the implementer is still mid-
flight on the first has no state transition (no ``iterating →
iterating``), so the projector accumulates the new feedback onto the
task row in place; the in-flight iteration consumes it on flush.
"""

DISPATCH_RESET_EVENTS = frozenset(
    {
        "SPEC.READY",
        "CRITIQUE.READY",
        "TASK.READY",
        "TASK.BLOCKED",
    },
)
"""Events that prove the prior dispatch reached the agent and ran.

Each one resets ``dispatch_failure_count`` to 0 on the row whose state
the projector advances. Advisor events (``REVIEW.READY``,
``TEST_REPORT.READY``) are not listed because the dispatches that
produce them are gated by an outer ``GuardedAdvance`` and don't
increment the counter in the first place.
"""


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def agentcore() -> Any:
    """Process-cached AgentCore data-plane client (memory CreateEvent)."""
    return boto3.client("bedrock-agentcore")


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def memory_id() -> str:
    """AgentCore Memory resource ID."""
    return os.environ["AIDLC_MEMORY_ID"]


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Fan out one EventBridge event into the read model + memory + outbox."""
    if "detail" in event and "detail-type" in event:
        return handle_eventbridge(event)
    logger.warning("unknown trigger shape", extra={"keys": sorted(event.keys())})
    return {"ok": False, "error": "unknown trigger"}


def handle_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Single EventBridge invocation; ``event['detail']`` is the envelope."""
    try:
        envelope = cast(
            "UntypedEnvelope",
            parse(
                event=normalise(event),
                model=UntypedEnvelope,
                envelope=EventBridgeEnvelope,
            ),
        )
    except ValidationError as exc:
        logger.warning("invalid event", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}
    detail = envelope.model_dump(mode="json")
    run_id = str(envelope.run_id)
    event_type = envelope.type
    committed = project_event(envelope=envelope, detail=detail)
    if committed:
        forward_to_memory(detail)
    metrics.add_metric(name="EventsProjected", unit=MetricUnit.Count, value=1)
    return {"ok": True, "run_id": run_id, "type": event_type, "committed": committed}


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode ``detail`` if EventBridge ships it as a JSON string."""
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


# ---------------------------------------------------------------------------
# Projection: one TransactWriteItems per event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMode:
    """How a run-level event affects the STATE row.

    ``from_state`` is what we read off the row before building the
    transaction; ``next_state`` is the target if the event advances
    state. ``accumulates_feedback`` is True when the event appends to
    ``pending_spec_feedback`` (SPEC.ITERATION_REQUESTED in the right
    states), independent of whether it also advances.
    """

    from_state: RunState | None
    next_state: RunState | None = None
    accumulates_feedback: bool = False

    @classmethod
    def for_event_without_run_transition(cls) -> RunMode:
        """The RunMode for events whose primary write is the TASK row.

        Task-level events (TASK.READY, TASK.BLOCKED, etc.) still touch
        the STATE row to record the event's timestamp, last_event_id,
        status string, and usage totals so the timeline + per-run
        counters keep rolling up. Those updates carry no
        ConditionExpression — the EVENT row's
        ``attribute_not_exists(sk)`` already gates re-delivery.
        """
        return cls(from_state=None, next_state=None, accumulates_feedback=False)


@dataclass(frozen=True)
class TaskMode:
    """How a task-level event affects the TASK row."""

    task_id: str
    from_state: TaskState
    next_state: TaskState | None = None
    accumulates_feedback: bool = False


@tracer.capture_method
def project_event(*, envelope: UntypedEnvelope, detail: dict[str, Any]) -> bool:
    """Build and commit one ``TransactWriteItems`` for this event.

    Returns ``True`` when the transaction committed, ``False`` on a
    conditional-check loss (re-delivery via the EVENT row, or a race
    loss on the STATE/TASK condition). On ``False`` the caller skips
    the AgentCore Memory write so memory is also idempotent on
    event_id.
    """
    run_id = str(envelope.run_id)
    event_type = envelope.type or "UNKNOWN"
    transaction = TransactWriteItemsBuilder()
    transaction.put(event_row_item(run_id, event_type, detail))
    if event_type in TASK_LEVEL_EVENTS:
        add_task_event_items(transaction, run_id, event_type, detail)
    else:
        add_run_event_items(transaction, run_id, event_type, detail)
    committed = transaction.commit(ddb())
    if not committed:
        logger.info(
            "event already projected (idempotent no-op)",
            extra={"run_id": run_id, "event_type": event_type},
        )
    return committed


def add_run_event_items(
    transaction: TransactWriteItemsBuilder,
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    """Add the STATE Update + optional OUTBOX Put for a run-level event."""
    current = read_state_attribute(
        pk=f"RUN#{run_id}",
        sk="STATE",
        attribute="current_state",
        enum_type=RunState,
        log_context={"run_id": run_id},
    )
    next_state = apply_run_transition(
        event_type=cast("PlatformEventType", event_type),
        current_state=current,
    )
    if event_type == "SPEC.ITERATION_REQUESTED":
        mode = spec_iteration_mode(current=current, next_state=next_state, detail=detail)
    else:
        mode = RunMode(from_state=current, next_state=next_state)
    transaction.update(run_state_item(run_id, event_type, detail, mode))
    if mode.next_state is not None:
        transaction.put(outbox_item(run_id, detail))


def spec_iteration_mode(
    *,
    current: RunState | None,
    next_state: RunState | None,
    detail: dict[str, Any],
) -> RunMode:
    """Decide whether SPEC.ITERATION_REQUESTED advances, accumulates, or drops.

    Late comments after merge / cancel land in a terminal state — drop
    them on the floor (no metadata update either, since the EVENT row
    Put unconditionally appends to the timeline regardless).
    """
    if current in TERMINAL_RUN_STATES:
        return RunMode(from_state=current)
    payload = detail.get("payload") or {}
    body = payload.get("feedback_body")
    if not isinstance(body, str) or not body.strip():
        return RunMode(from_state=current)
    advances = next_state == RunState.spec_pending and current is not None
    if not advances and current not in SPEC_ITERATION_ACCUMULATOR_STATES:
        return RunMode(from_state=current)
    return RunMode(
        from_state=current,
        next_state=next_state if advances else None,
        accumulates_feedback=True,
    )


def add_task_event_items(
    transaction: TransactWriteItemsBuilder,
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    """Add the STATE metadata + TASK row + optional OUTBOX items for a task event."""
    payload = detail.get("payload") or {}
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        logger.warning("task event missing task_id", extra={"event_type": event_type})
        transaction.update(
            run_state_item(
                run_id,
                event_type,
                detail,
                RunMode.for_event_without_run_transition(),
            ),
        )
        return
    current = read_state_attribute(
        pk=f"RUN#{run_id}",
        sk=f"TASK#{task_id}",
        attribute="status",
        enum_type=TaskState,
        log_context={"task_id": task_id, "run_id": run_id},
    )
    if current is None:
        logger.info(
            "task row missing — skipping task transition",
            extra={"run_id": run_id, "task_id": task_id, "event_type": event_type},
        )
        transaction.update(
            run_state_item(
                run_id,
                event_type,
                detail,
                RunMode.for_event_without_run_transition(),
            ),
        )
        return
    next_state = apply_task_transition(
        event_type=cast("PlatformEventType", event_type),
        current_state=current,
    )
    accumulates = (
        next_state is None
        and event_type == "TASK.ITERATION_REQUESTED"
        and current in ITERATION_ACCUMULATOR_STATES
    )
    mode = TaskMode(
        task_id=task_id,
        from_state=current,
        next_state=next_state,
        accumulates_feedback=accumulates,
    )
    transaction.update(
        run_state_item(
            run_id,
            event_type,
            detail,
            RunMode.for_event_without_run_transition(),
        ),
    )
    if next_state is not None or accumulates:
        transaction.update(task_row_item(run_id, event_type, detail, mode))
    if next_state is not None:
        transaction.put(outbox_item(run_id, detail))


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def event_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> PutBuilder:
    """The EVENT timeline row — master idempotency key for the event.

    A re-delivered envelope fails ``attribute_not_exists(sk)`` and the
    entire transaction rolls back, which is what makes downstream
    metadata + state writes safe under at-least-once delivery.
    """
    event_id = detail.get("event_id", "unknown")
    return PutBuilder(
        table=runs_table(),
        item={
            "pk": f"RUN#{run_id}",
            "sk": f"EVENT#{event_id}",
            "type": event_type,
            "envelope": json.dumps(detail),
        },
    ).condition_not_exists("sk")


def run_state_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
    mode: RunMode,
) -> UpdateBuilder:
    """Build the STATE row Update for this event.

    Composes always-on metadata (status, timestamps, last_event_*),
    payload projections (project_slug, GSI keys, spec_slug, etc.),
    usage totals, optional state-advance, optional spec-feedback
    accumulator, and the right ConditionExpression onto a single
    UpdateBuilder.

    Pure metadata-only updates carry no ConditionExpression — the
    EVENT row's ``attribute_not_exists(sk)`` already gates re-delivery.
    Advancing or accumulator events condition on the cursor's exact
    value (or ``attribute_not_exists(current_state)`` for the first
    event) so a concurrent event that just moved past drops cleanly
    via CCFE.
    """
    payload = detail.get("payload") or {}
    update = (
        UpdateBuilder(
            table=runs_table(),
            key={"pk": f"RUN#{run_id}", "sk": "STATE"},
        )
        .set("status", event_type)
        .set("updated_at", detail.get("timestamp", ""))
        .set("last_event_id", detail.get("event_id", ""))
        .set("last_event_at", detail.get("timestamp", ""))
    )
    apply_payload_projections(update, run_id=run_id, event_type=event_type, payload=payload)
    apply_usage_totals(update, payload=payload)
    apply_run_state_advance(update, mode=mode, event_type=event_type)
    apply_spec_feedback_accumulator(update, mode=mode, payload=payload)
    apply_run_state_condition(update, mode=mode)
    return update


def apply_payload_projections(
    update: UpdateBuilder,
    *,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Project payload fields onto the STATE row.

    Covers the always-on fields (``project_slug`` via if_not_exists,
    the ``REQUEST.RECEIVED`` GSI keys, ``spec_slug``,
    ``spec_s3_prefix``) and the per-event-type fields
    (``ISSUE.TRIAGED`` workflow_kind + triage_action, ``SPEC.READY``
    tasks_total + task_ids, ``RUN.COMPLETED`` tasks_completed). Each
    is gated on the payload field being well-formed.
    """
    project_slug = payload.get("project_slug")
    if isinstance(project_slug, str) and project_slug:
        update.set_if_not_exists("project_slug", project_slug)
    source_issue_url = payload.get("source_issue_url")
    if event_type == "REQUEST.RECEIVED" and isinstance(source_issue_url, str) and source_issue_url:
        update.set_if_not_exists("gsi1pk", f"ISSUE#{source_issue_url}")
        update.set_if_not_exists("gsi1sk", f"RUN#{run_id}")
        update.set_if_not_exists("source_issue_url", source_issue_url)
    spec_slug = payload.get("spec_slug")
    if isinstance(spec_slug, str) and spec_slug:
        update.set("spec_slug", spec_slug)
    spec_s3_prefix = payload.get("spec_s3_prefix")
    if isinstance(spec_s3_prefix, str) and spec_s3_prefix:
        update.set("spec_s3_prefix", spec_s3_prefix)
    apply_event_specific_projections(update, event_type=event_type, payload=payload)


def apply_event_specific_projections(
    update: UpdateBuilder,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Project fields that exist only for specific event types.

    ISSUE.TRIAGED carries the chosen workflow + the decision verb;
    SPEC.READY carries the task count + task ID set the dashboard
    renders; RUN.COMPLETED carries the final tasks_completed counter
    for the run summary.
    """
    if event_type == "ISSUE.TRIAGED":
        workflow_kind = payload.get("workflow_kind")
        if isinstance(workflow_kind, str) and workflow_kind:
            update.set("workflow_kind", workflow_kind)
        action = payload.get("action")
        if isinstance(action, str) and action:
            update.set("triage_action", action)
    elif event_type == "SPEC.READY":
        task_count = payload.get("task_count")
        if isinstance(task_count, int):
            update.set("tasks_total", task_count)
        task_ids = payload.get("task_ids")
        if isinstance(task_ids, list) and task_ids and all(isinstance(t, str) for t in task_ids):
            update.set("task_ids", set(task_ids))
    elif event_type == "RUN.COMPLETED":
        update.set("tasks_completed", int(payload.get("tasks_completed", 0)))


def apply_usage_totals(update: UpdateBuilder, *, payload: dict[str, Any]) -> None:
    """ADD per-event token / cost / duration totals when non-zero.

    Each ADD is conditional on the value being non-zero to avoid a
    no-op DDB write. Float ``cost_usd`` passes through the builder
    which Decimal-normalises before serialisation.
    """
    in_tokens = int(payload.get("token_in", 0) or 0)
    if in_tokens:
        update.add("total_token_in", in_tokens)
    out_tokens = int(payload.get("token_out", 0) or 0)
    if out_tokens:
        update.add("total_token_out", out_tokens)
    cost = float(payload.get("cost_usd", 0.0) or 0.0)
    if cost:
        update.add("total_cost_usd", cost)
    duration = int(payload.get("duration_ms", 0) or 0)
    if duration:
        update.add("total_duration_ms", duration)


def apply_run_state_advance(
    update: UpdateBuilder,
    *,
    mode: RunMode,
    event_type: str,
) -> None:
    """Advance ``current_state`` when ``mode.next_state`` is set.

    SET ``current_state``, ADD ``state_transitions``, optionally reset
    ``dispatch_failure_count`` for dispatch-completion events, and on
    ``SPEC.READY`` arriving from ``architect_running`` clear
    ``pending_spec_feedback`` and REMOVE ``spec_delivery_ids`` (the
    architect cycle consumed them).
    """
    if mode.next_state is None:
        return
    update.set("current_state", mode.next_state.value)
    update.add("state_transitions", 1)
    if event_type in DISPATCH_RESET_EVENTS:
        update.set("dispatch_failure_count", 0)
    if event_type == "SPEC.READY" and mode.from_state == RunState.architect_running:
        update.set("pending_spec_feedback", [])
        update.remove("spec_delivery_ids")


def apply_spec_feedback_accumulator(
    update: UpdateBuilder,
    *,
    mode: RunMode,
    payload: dict[str, Any],
) -> None:
    """Append SPEC.ITERATION_REQUESTED feedback onto ``pending_spec_feedback``.

    Fires only when ``mode.accumulates_feedback``. The delivery_id is
    added to the dedupe ``spec_delivery_ids`` set so the architect can
    skip already-consumed deliveries on re-run.
    """
    if not mode.accumulates_feedback:
        return
    body = payload.get("feedback_body")
    if not isinstance(body, str) or not body.strip():
        return
    update.list_append("pending_spec_feedback", [body])
    delivery_id = payload.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        update.add("spec_delivery_ids", {delivery_id})


def apply_run_state_condition(update: UpdateBuilder, *, mode: RunMode) -> None:
    """Attach the STATE row's ConditionExpression.

    Pure metadata-only updates carry no condition — the EVENT row's
    ``attribute_not_exists(sk)`` already gates re-delivery. Advancing
    or accumulator events condition on the cursor's exact value (or
    ``attribute_not_exists(current_state)`` for the first event) so a
    concurrent event that just moved past drops cleanly via CCFE.
    """
    if mode.next_state is None and not mode.accumulates_feedback:
        return
    if mode.from_state is None:
        update.condition_not_exists("current_state")
    else:
        update.condition_eq("current_state", mode.from_state.value)


def task_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
    mode: TaskMode,
) -> UpdateBuilder:
    """Build the TASK row Update — state advance + iteration accumulator + pr_url.

    The condition ``status = :from`` is always attached; a re-delivery
    or race that lost the cursor drops cleanly via CCFE.
    """
    payload = detail.get("payload") or {}
    update = (
        UpdateBuilder(
            table=runs_table(),
            key={"pk": f"RUN#{run_id}", "sk": f"TASK#{mode.task_id}"},
        )
        .set("last_event_id", detail.get("event_id", ""))
        .set("last_event_at", detail.get("timestamp", ""))
        .condition_eq("status", mode.from_state.value)
    )
    apply_task_state_advance(update, mode=mode, event_type=event_type)
    apply_task_iteration_clauses(update, event_type=event_type, payload=payload)
    pr_url = payload.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        update.set("pr_url", pr_url)
    return update


def apply_task_state_advance(
    update: UpdateBuilder,
    *,
    mode: TaskMode,
    event_type: str,
) -> None:
    """SET status to next state, reset dispatch counter, flush on iterating→pr_open.

    On ``TASK.READY`` arriving from ``iterating``, the just-merged
    commit consumed the queued feedback — clear ``pending_feedback``
    and REMOVE ``delivery_ids`` so the next iteration starts fresh.
    """
    if mode.next_state is None:
        return
    update.set("status", mode.next_state.value)
    if event_type in DISPATCH_RESET_EVENTS:
        update.set("dispatch_failure_count", 0)
    if event_type == "TASK.READY" and mode.from_state == TaskState.iterating:
        update.set("pending_feedback", [])
        update.remove("delivery_ids")


def apply_task_iteration_clauses(
    update: UpdateBuilder,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Append the feedback dict to ``pending_feedback`` and ADD the delivery_id.

    Fires on every ``TASK.ITERATION_REQUESTED`` — whether or not the
    event also advances task state. The feedback dict serialises to a
    DDB Map directly via ``TypeSerializer``; bool / int / None fields
    are disambiguated correctly without per-type coercion.
    """
    if event_type != "TASK.ITERATION_REQUESTED":
        return
    feedback = payload.get("feedback")
    if isinstance(feedback, dict):
        update.list_append("pending_feedback", [feedback])
    delivery_id = payload.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        update.add("delivery_ids", {delivery_id})


def outbox_item(run_id: str, detail: dict[str, Any]) -> PutBuilder:
    """The OUTBOX row the EventBridge Pipe forwards to the beacon queue."""
    event_id = detail.get("event_id", "")
    return PutBuilder(
        table=runs_table(),
        item={
            "pk": f"RUN#{run_id}",
            "sk": f"OUTBOX#{event_id}",
            "run_id": run_id,
            "project_slug": project_slug_from_envelope(detail=detail, run_id=run_id),
            "expire_at": int(datetime.now(UTC).timestamp()) + OUTBOX_TTL_SECONDS,
        },
    ).condition_not_exists("sk")


def project_slug_from_envelope(*, detail: dict[str, Any], run_id: str) -> str:
    """Read ``project_slug`` from the event payload; fall back to ``run_id``.

    The beacon queue is a Standard queue with SQS fair-queue grouping;
    the pipe sets ``MessageGroupId`` to the row's ``project_slug`` so
    noisy-neighbor metrics are reported per project. The fallback to
    ``run_id`` keeps the outbox write defensible if a future event
    omits ``project_slug``.
    """
    payload = detail.get("payload") or {}
    slug = payload.get("project_slug")
    if isinstance(slug, str) and slug:
        return slug
    return run_id


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------


def read_state_attribute[T: StrEnum](
    *,
    pk: str,
    sk: str,
    attribute: str,
    enum_type: type[T],
    log_context: dict[str, str],
) -> T | None:
    """Read one STR attribute off a runs-table row and parse it as a StrEnum.

    Returns ``None`` if the row is missing, the attribute is absent or
    empty, or the value can't be parsed by ``enum_type``. Aliases the
    attribute name via ``ExpressionAttributeNames`` so reserved-word
    attributes (``status``) work without per-call special-casing.
    """
    response = ddb().get_item(
        TableName=runs_table(),
        Key={"pk": {"S": pk}, "sk": {"S": sk}},
        ProjectionExpression="#a",
        ExpressionAttributeNames={"#a": attribute},
    )
    item = response.get("Item")
    if not item:
        return None
    decoded = deserialize_item(cast("dict[str, Any]", item))
    raw = decoded.get(attribute)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return enum_type(raw)
    except ValueError:
        logger.warning(
            "unknown attribute value in DDB",
            extra={"attribute": attribute, "raw": raw, **log_context},
        )
        return None


# ---------------------------------------------------------------------------
# AgentCore Memory pass-through
# ---------------------------------------------------------------------------


@tracer.capture_method
def forward_to_memory(envelope: dict[str, Any]) -> None:
    """Emit the envelope to AgentCore Memory as a CreateEvent.

    Only invoked after the projector's transaction commits, so memory
    writes are idempotent on event_id (a re-delivery rolls back the
    transaction and the projector returns early before reaching here).

    AgentCore Memory's ``CreateEvent`` requires ``eventTimestamp`` and
    a ``payload`` whose entries are a tagged union of ``conversational``
    or ``blob``. ``blob`` is a JSON-compatible Document, not raw bytes
    — we pass the envelope dict directly.
    """
    actor_id = envelope.get("payload", {}).get("project_slug") or envelope.get("actor_id", "system")
    session_id = envelope.get("run_id", "system")
    try:
        agentcore().create_event(
            memoryId=memory_id(),
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=parse_event_timestamp(envelope),
            payload=[{"blob": envelope}],
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning("memory CreateEvent failed", extra={"err": repr(exc)})
        metrics.add_metric(name="MemoryWriteFailures", unit=MetricUnit.Count, value=1)


def parse_event_timestamp(envelope: dict[str, Any]) -> datetime:
    """Parse the envelope's ISO-8601 ``timestamp`` field for boto3."""
    raw = envelope.get("timestamp")
    if isinstance(raw, str):
        return datetime.fromisoformat(raw)
    return datetime.now(UTC)
