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
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

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
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

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
    items: list[TransactWriteItemTypeDef] = [event_row_item(run_id, event_type, detail)]
    if event_type in TASK_LEVEL_EVENTS:
        items.extend(task_event_items(run_id, event_type, detail))
    else:
        items.extend(run_event_items(run_id, event_type, detail))
    try:
        ddb().transact_write_items(TransactItems=items)
    except ClientError as exc:
        if is_conditional_check_failed(exc):
            logger.info(
                "event already projected (idempotent no-op)",
                extra={"run_id": run_id, "event_type": event_type},
            )
            return False
        raise
    return True


def run_event_items(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> list[TransactWriteItemTypeDef]:
    """Build the STATE Update + optional OUTBOX Put for a run-level event."""
    current = read_run_state(run_id)
    next_state = apply_run_transition(
        event_type=cast("PlatformEventType", event_type),
        current_state=current,
    )
    if event_type == "SPEC.ITERATION_REQUESTED":
        mode = spec_iteration_mode(current=current, next_state=next_state, detail=detail)
    else:
        mode = RunMode(from_state=current, next_state=next_state)
    items: list[TransactWriteItemTypeDef] = [run_state_item(run_id, event_type, detail, mode)]
    if mode.next_state is not None:
        items.append(outbox_item(run_id, detail))
    return items


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


def task_event_items(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> list[TransactWriteItemTypeDef]:
    """STATE metadata Update + TASK row Update + optional OUTBOX Put."""
    payload = detail.get("payload") or {}
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        logger.warning("task event missing task_id", extra={"event_type": event_type})
        return [run_state_item(run_id, event_type, detail, RunMode(from_state=None))]
    current = read_task_state(run_id, task_id)
    if current is None:
        logger.info(
            "task row missing — skipping task transition",
            extra={"run_id": run_id, "task_id": task_id, "event_type": event_type},
        )
        return [run_state_item(run_id, event_type, detail, RunMode(from_state=None))]
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
    items: list[TransactWriteItemTypeDef] = [
        run_state_item(run_id, event_type, detail, RunMode(from_state=None)),
    ]
    if next_state is not None or accumulates:
        items.append(task_row_item(run_id, event_type, detail, mode))
    if next_state is not None:
        items.append(outbox_item(run_id, detail))
    return items


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def event_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> TransactWriteItemTypeDef:
    """The EVENT timeline row — master idempotency key for the event.

    A re-delivered envelope fails ``attribute_not_exists(sk)`` and the
    entire transaction rolls back, which is what makes downstream
    metadata + state writes safe under at-least-once delivery.
    """
    event_id = detail.get("event_id", "unknown")
    return {
        "Put": {
            "TableName": runs_table(),
            "Item": {
                "pk": {"S": f"RUN#{run_id}"},
                "sk": {"S": f"EVENT#{event_id}"},
                "type": {"S": event_type},
                "envelope": {"S": json.dumps(detail)},
            },
            "ConditionExpression": "attribute_not_exists(sk)",
        },
    }


def run_state_item(  # noqa: C901, PLR0912, PLR0915
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
    mode: RunMode,
) -> TransactWriteItemTypeDef:
    """The STATE row Update — every clause this row needs in one Update.

    The branching is intrinsic to a STATE row that mixes always-on
    metadata (status, updated_at, if-not-exists project_slug, GSI keys
    for issue-driven REQUEST.RECEIVED, per-event-type projections,
    usage totals via ``ADD``) with optional state-advance and spec-
    iteration-feedback clauses. Splitting into single-call helpers
    only added indirection (every helper had exactly one caller),
    so the linter suppressions accept that this is one cohesive
    item-builder rather than separable concerns.

    Condition policy: pure metadata-only updates carry no
    ``ConditionExpression`` — the EVENT row's
    ``attribute_not_exists(sk)`` already gates re-delivery. Advancing
    or accumulator events condition on the cursor's exact value (or
    ``attribute_not_exists(current_state)`` for the first event) so a
    concurrent event that just moved past drops cleanly via CCFE.
    """
    payload = detail.get("payload") or {}
    timestamp = detail.get("timestamp", "")
    event_id = detail.get("event_id", "")
    set_parts = [
        "#s = :status",
        "updated_at = :ts",
        "last_event_id = :eid",
        "last_event_at = :ts",
    ]
    add_parts: list[str] = []
    remove_parts: list[str] = []
    names: dict[str, str] = {"#s": "status"}
    values: dict[str, dict[str, Any]] = {
        ":status": {"S": event_type},
        ":ts": {"S": timestamp},
        ":eid": {"S": event_id},
    }
    project_slug = payload.get("project_slug")
    if isinstance(project_slug, str) and project_slug:
        set_parts.append("project_slug = if_not_exists(project_slug, :proj)")
        values[":proj"] = {"S": project_slug}
    source_issue_url = payload.get("source_issue_url")
    if event_type == "REQUEST.RECEIVED" and isinstance(source_issue_url, str) and source_issue_url:
        set_parts.append("gsi1pk = if_not_exists(gsi1pk, :issue)")
        set_parts.append("gsi1sk = if_not_exists(gsi1sk, :runref)")
        set_parts.append("source_issue_url = if_not_exists(source_issue_url, :issue_url)")
        values[":issue"] = {"S": f"ISSUE#{source_issue_url}"}
        values[":runref"] = {"S": f"RUN#{run_id}"}
        values[":issue_url"] = {"S": source_issue_url}
    spec_slug = payload.get("spec_slug")
    if isinstance(spec_slug, str) and spec_slug:
        set_parts.append("spec_slug = :spec_slug")
        values[":spec_slug"] = {"S": spec_slug}
    spec_s3_prefix = payload.get("spec_s3_prefix")
    if isinstance(spec_s3_prefix, str) and spec_s3_prefix:
        set_parts.append("spec_s3_prefix = :spec_prefix")
        values[":spec_prefix"] = {"S": spec_s3_prefix}
    if event_type == "ISSUE.TRIAGED":
        workflow_kind = payload.get("workflow_kind")
        if isinstance(workflow_kind, str) and workflow_kind:
            set_parts.append("workflow_kind = :wk")
            values[":wk"] = {"S": workflow_kind}
        action = payload.get("action")
        if isinstance(action, str) and action:
            set_parts.append("triage_action = :ta")
            values[":ta"] = {"S": action}
    if event_type == "SPEC.READY":
        if isinstance(payload.get("task_count"), int):
            set_parts.append("tasks_total = :tt")
            values[":tt"] = {"N": str(payload["task_count"])}
        task_ids = payload.get("task_ids")
        if isinstance(task_ids, list) and task_ids and all(isinstance(t, str) for t in task_ids):
            set_parts.append("task_ids = :tids")
            values[":tids"] = {"SS": list(task_ids)}
    if event_type == "RUN.COMPLETED":
        set_parts.append("tasks_completed = :tc")
        values[":tc"] = {"N": str(int(payload.get("tasks_completed", 0)))}
    in_tokens = int(payload.get("token_in", 0) or 0)
    out_tokens = int(payload.get("token_out", 0) or 0)
    cost = float(payload.get("cost_usd", 0.0) or 0.0)
    duration = int(payload.get("duration_ms", 0) or 0)
    if in_tokens:
        add_parts.append("total_token_in :ti")
        values[":ti"] = {"N": str(in_tokens)}
    if out_tokens:
        add_parts.append("total_token_out :to_")
        values[":to_"] = {"N": str(out_tokens)}
    if cost:
        add_parts.append("total_cost_usd :cost")
        values[":cost"] = {"N": str(cost)}
    if duration:
        add_parts.append("total_duration_ms :dur")
        values[":dur"] = {"N": str(duration)}
    if mode.next_state is not None:
        set_parts.append("current_state = :to")
        values[":to"] = {"S": mode.next_state.value}
        add_parts.append("state_transitions :one")
        values[":one"] = {"N": "1"}
        if event_type in DISPATCH_RESET_EVENTS:
            set_parts.append("dispatch_failure_count = :zero")
            values[":zero"] = {"N": "0"}
        if event_type == "SPEC.READY" and mode.from_state == RunState.architect_running:
            set_parts.append("pending_spec_feedback = :feedback_empty_advance")
            values[":feedback_empty_advance"] = {"L": []}
            remove_parts.append("spec_delivery_ids")
    if mode.accumulates_feedback:
        body = payload.get("feedback_body")
        if isinstance(body, str) and body.strip():
            set_parts.append(
                "pending_spec_feedback = list_append("
                "if_not_exists(pending_spec_feedback, :feedback_empty), :feedback_one)",
            )
            values[":feedback_empty"] = {"L": []}
            values[":feedback_one"] = {"L": [{"S": body}]}
            delivery_id = payload.get("delivery_id")
            if isinstance(delivery_id, str) and delivery_id:
                add_parts.append("spec_delivery_ids :spec_did")
                values[":spec_did"] = {"SS": [delivery_id]}
    if mode.next_state is None and not mode.accumulates_feedback:
        condition = None
    elif mode.from_state is None:
        condition = "attribute_not_exists(current_state)"
    else:
        condition = "current_state = :from"
        values[":from"] = {"S": mode.from_state.value}
    expression = compose_expression(set_parts, add_parts, remove_parts)
    update: dict[str, Any] = {
        "TableName": runs_table(),
        "Key": {"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if condition is not None:
        update["ConditionExpression"] = condition
    return cast("TransactWriteItemTypeDef", {"Update": update})


def task_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
    mode: TaskMode,
) -> TransactWriteItemTypeDef:
    """The TASK row Update — advance or in-place iteration accumulator.

    On ``TASK.READY`` arriving from ``iterating``, also flushes the
    feedback queue (the just-merged commit consumed it). On
    ``TASK.ITERATION_REQUESTED``, appends to ``pending_feedback`` +
    ADDs the delivery_id (whether or not the event also advances
    state).
    """
    payload = detail.get("payload") or {}
    timestamp = detail.get("timestamp", "")
    set_parts = ["last_event_id = :eid", "last_event_at = :ts"]
    add_parts: list[str] = []
    remove_parts: list[str] = []
    values: dict[str, Any] = {
        ":from": {"S": mode.from_state.value},
        ":eid": {"S": detail.get("event_id", "")},
        ":ts": {"S": timestamp},
    }
    names: dict[str, str] = {"#s": "status"}
    if mode.next_state is not None:
        set_parts.insert(0, "#s = :to")
        values[":to"] = {"S": mode.next_state.value}
        if event_type in DISPATCH_RESET_EVENTS:
            set_parts.append("dispatch_failure_count = :zero")
            values[":zero"] = {"N": "0"}
        if event_type == "TASK.READY" and mode.from_state == TaskState.iterating:
            set_parts.append("pending_feedback = :empty_list")
            values[":empty_list"] = {"L": []}
            remove_parts.append("delivery_ids")
    if event_type == "TASK.ITERATION_REQUESTED":
        feedback = payload.get("feedback")
        if isinstance(feedback, dict):
            set_parts.append(
                "pending_feedback = list_append("
                "if_not_exists(pending_feedback, :feedback_empty), :feedback_one"
                ")",
            )
            values[":feedback_empty"] = {"L": []}
            values[":feedback_one"] = {"L": [{"M": map_feedback(feedback)}]}
        delivery_id = payload.get("delivery_id")
        if isinstance(delivery_id, str) and delivery_id:
            add_parts.append("delivery_ids :did")
            values[":did"] = {"SS": [delivery_id]}
    pr_url = payload.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        set_parts.append("pr_url = :pr_url")
        values[":pr_url"] = {"S": pr_url}
    expression = compose_expression(set_parts, add_parts, remove_parts)
    return {
        "Update": {
            "TableName": runs_table(),
            "Key": {
                "pk": {"S": f"RUN#{run_id}"},
                "sk": {"S": f"TASK#{mode.task_id}"},
            },
            "UpdateExpression": expression,
            "ConditionExpression": "#s = :from",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        },
    }


def outbox_item(run_id: str, detail: dict[str, Any]) -> TransactWriteItemTypeDef:
    """The OUTBOX row the EventBridge Pipe forwards to the beacon queue."""
    event_id = detail.get("event_id", "")
    project_slug = project_slug_from_envelope(detail=detail, run_id=run_id)
    expire_at = int(datetime.now(UTC).timestamp()) + OUTBOX_TTL_SECONDS
    return {
        "Put": {
            "TableName": runs_table(),
            "Item": {
                "pk": {"S": f"RUN#{run_id}"},
                "sk": {"S": f"OUTBOX#{event_id}"},
                "run_id": {"S": run_id},
                "project_slug": {"S": project_slug},
                "expire_at": {"N": str(expire_at)},
            },
            "ConditionExpression": "attribute_not_exists(sk)",
        },
    }


# ---------------------------------------------------------------------------
# Expression builders
# ---------------------------------------------------------------------------


def compose_expression(
    set_parts: list[str],
    add_parts: list[str],
    remove_parts: list[str],
) -> str:
    """Stitch SET / ADD / REMOVE clauses into one UpdateExpression."""
    expression = "SET " + ", ".join(set_parts)
    if add_parts:
        expression += " ADD " + ", ".join(add_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)
    return expression


def map_feedback(feedback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert a flat feedback dict into a one-level DDB map.

    All FeedbackItem fields are scalars (str / int / bool); nested
    structures don't exist today. If a future FeedbackItem grows
    nested state, extend this function.
    """
    out: dict[str, dict[str, Any]] = {}
    for k, v in feedback.items():
        if isinstance(v, str):
            out[k] = {"S": v}
        elif isinstance(v, bool):
            out[k] = {"BOOL": v}
        elif isinstance(v, int):
            out[k] = {"N": str(v)}
        elif v is None:
            out[k] = {"NULL": True}
    return out


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


def is_conditional_check_failed(exc: ClientError) -> bool:
    """``True`` when a TransactWriteItems was cancelled by a condition mismatch.

    Surfaces as ``TransactionCanceledException`` with a per-item
    ``CancellationReasons`` list. Any ``ConditionalCheckFailed`` reason
    means we lost a race or this is a re-delivery — both silent no-ops.
    """
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons", []) or []
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------


def read_run_state(run_id: str) -> RunState | None:
    """Read ``current_state`` off the run's STATE row, or ``None``."""
    item = (
        ddb()
        .get_item(
            TableName=runs_table(),
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
            ProjectionExpression="current_state",
        )
        .get("Item")
    )
    if not item:
        return None
    raw = item.get("current_state", {}).get("S")
    if not raw:
        return None
    try:
        return RunState(raw)
    except ValueError:
        logger.warning("unknown run state in DDB", extra={"raw": raw, "run_id": run_id})
        return None


def read_task_state(run_id: str, task_id: str) -> TaskState | None:
    """Read ``status`` off a TASK row, or ``None`` if the row is missing."""
    item = (
        ddb()
        .get_item(
            TableName=runs_table(),
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": f"TASK#{task_id}"}},
            ProjectionExpression="#s",
            ExpressionAttributeNames={"#s": "status"},
        )
        .get("Item")
    )
    if not item:
        return None
    raw = item.get("status", {}).get("S")
    if not raw:
        return None
    try:
        return TaskState(raw)
    except ValueError:
        logger.warning("unknown task state in DDB", extra={"raw": raw, "task_id": task_id})
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
    except Exception as exc:
        logger.warning("memory CreateEvent failed", extra={"err": repr(exc)})
        metrics.add_metric(name="MemoryWriteFailures", unit=MetricUnit.Count, value=1)


def parse_event_timestamp(envelope: dict[str, Any]) -> datetime:
    """Parse the envelope's ISO-8601 ``timestamp`` field for boto3."""
    raw = envelope.get("timestamp")
    if isinstance(raw, str):
        return datetime.fromisoformat(raw)
    return datetime.now(UTC)
