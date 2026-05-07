"""Projector Lambda — fans out platform events into the read model + memory.

Two trigger sources, dispatched by event shape:

* **EventBridge rule** — every event on the platform bus. We update the run
  state row in DynamoDB (so the dashboard's SSE poller has fresh data) and
  forward the event to AgentCore Memory via ``CreateEvent`` so cross-session
  memory strategies can index it.

* **DynamoDB Streams** (runs + approvals tables) — currently a no-op
  passthrough; surfaces here so the wiring exists when a stream consumer
  is needed. Routed through Powertools' ``BatchProcessor`` so a single
  poison record reports only itself as a partial failure rather than
  poison-pilling the whole batch.

The projector is the **only writer** of the run + task state-machine
cursors: it applies :func:`common.state_transitions.apply_run_transition`
/ ``apply_task_transition`` and writes the new state via a conditional
``UpdateItem``.

The projector is idempotent: every write uses the event_id as a sort-key
suffix or a ``ConditionExpression`` that keeps repeats from clobbering
already-applied state.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import (
    DynamoDBRecord,
)
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from common.events import EventType as PlatformEventType
from common.events import UntypedEnvelope
from common.state import RunState, TaskState
from common.state_transitions import apply_run_transition, apply_task_transition

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="event_projector")
tracer = Tracer(service="event_projector")
metrics = Metrics(namespace="ai-dlc", service="event_projector")

stream_processor = BatchProcessor(event_type=EventType.DynamoDBStreams)


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
def handler(
    event: dict[str, Any],
    context: LambdaContext,
) -> dict[str, Any] | PartialItemFailureResponse:
    """Fan out one EventBridge event or one DDB-Stream batch to consumers."""
    if "Records" in event:
        return process_partial_response(
            event=event,
            record_handler=process_stream_record,
            processor=stream_processor,
            context=context,
        )
    if "detail" in event and "detail-type" in event:
        return handle_eventbridge(event)
    logger.warning("unknown trigger shape", extra={"keys": sorted(event.keys())})
    return {"ok": False, "error": "unknown trigger"}


def handle_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Single EventBridge invocation; ``event['detail']`` is the envelope."""
    try:
        # parse() is typed to allow batch shapes; EventBridgeEnvelope always
        # returns the single inner model, hence the cast.
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
    upsert_run_event(run_id=run_id, event_type=event_type, envelope=detail)
    update_run_state(run_id=run_id, event_type=event_type, envelope=detail)
    apply_state_transition(run_id=run_id, event_type=event_type, envelope=detail)
    forward_to_memory(detail)
    metrics.add_metric(name="EventsProjected", unit=MetricUnit.Count, value=1)
    return {"ok": True, "run_id": run_id, "type": event_type}


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode ``detail`` if EventBridge ships it as a JSON string."""
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


def process_stream_record(record: DynamoDBRecord) -> None:
    """Per-record handler for the DDB Streams branch.

    Currently a no-op pass-through; raising propagates to the BatchProcessor
    which records the record's sequence number under ``batchItemFailures``
    so DDB Streams retries only the failed record.
    """
    seq = record.dynamodb.sequence_number if record.dynamodb else None
    logger.debug(
        "ddb stream record",
        extra={"event_name": record.event_name, "seq": seq},
    )


@tracer.capture_method
def upsert_run_event(*, run_id: str, event_type: str | None, envelope: dict[str, Any]) -> None:
    """Append the event to the run's timeline row, idempotent on event_id.

    EventBridge can redeliver the same envelope (transient downstream
    failure, retry-on-error, at-least-once semantics). The
    ``attribute_not_exists(sk)`` guard makes the PutItem a no-op on
    repeat, but DDB surfaces that as ``ConditionalCheckFailedException``
    — we swallow it so the rest of the projection (state update +
    transition + memory) can run on retries that need to make forward
    progress past a previously-failing later step.
    """
    event_id = envelope.get("event_id", "unknown")
    try:
        ddb().put_item(
            TableName=runs_table(),
            Item={
                "pk": {"S": f"RUN#{run_id}"},
                "sk": {"S": f"EVENT#{envelope.get('timestamp', '')}#{event_id}"},
                "type": {"S": event_type or "UNKNOWN"},
                "envelope": {"S": json.dumps(envelope)},
            },
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(
                "event row already exists; continuing projection",
                extra={"run_id": run_id, "event_id": event_id, "event_type": event_type},
            )
            return
        raise


@tracer.capture_method
def update_run_state(*, run_id: str, event_type: str | None, envelope: dict[str, Any]) -> None:
    """Upsert the run's STATE row with current status + cumulative metrics.

    The STATE row at sk=`STATE` is what the dashboard reads. We update it
    on every event so the runs list stays current; usage counters
    (``total_token_in``/``out``, ``total_cost_usd``, ``total_duration_ms``)
    are accumulated via DynamoDB ``ADD`` whenever an agent ``*.READY``
    event carries them. ``RUN.COMPLETED`` only sets ``tasks_completed``;
    the totals are already canonical from accumulation.
    """
    payload = envelope.get("payload") or {}
    set_parts = ["#s = :status", "updated_at = :ts"]
    add_parts: list[str] = []
    names = {"#s": "status"}
    values: dict[str, dict[str, Any]] = {
        ":status": {"S": event_type or "UNKNOWN"},
        ":ts": {"S": envelope.get("timestamp", "")},
    }
    if isinstance(payload.get("project_slug"), str) and payload["project_slug"]:
        set_parts.append("project_slug = if_not_exists(project_slug, :proj)")
        values[":proj"] = {"S": payload["project_slug"]}
    accumulate_spec_fields(payload, set_parts=set_parts, values=values)
    # REQUEST.RECEIVED for an issue-driven run carries source_issue_url.
    # Project the URL onto the gsi1 keys so the dashboard can look up
    # in-flight runs by issue (used by the issues.unassigned cancel path).
    if (
        event_type == "REQUEST.RECEIVED"
        and isinstance(payload.get("source_issue_url"), str)
        and payload["source_issue_url"]
    ):
        set_parts.append("gsi1pk = if_not_exists(gsi1pk, :issue)")
        set_parts.append("gsi1sk = if_not_exists(gsi1sk, :runref)")
        set_parts.append("source_issue_url = if_not_exists(source_issue_url, :issue_url)")
        values[":issue"] = {"S": f"ISSUE#{payload['source_issue_url']}"}
        values[":runref"] = {"S": f"RUN#{run_id}"}
        values[":issue_url"] = {"S": payload["source_issue_url"]}
    accumulate_usage(payload, add_parts=add_parts, values=values)
    if event_type == "SPEC.READY":
        accumulate_spec_ready(payload, set_parts=set_parts, values=values)
    if event_type == "ISSUE.TRIAGED":
        accumulate_issue_triaged(payload, set_parts=set_parts, values=values)
    if event_type == "RUN.COMPLETED":
        set_parts.append("tasks_completed = :tc")
        values[":tc"] = {"N": str(int(payload.get("tasks_completed", 0)))}
    expression = "SET " + ", ".join(set_parts)
    if add_parts:
        expression += " ADD " + ", ".join(add_parts)
    ddb().update_item(
        TableName=runs_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def accumulate_spec_fields(
    payload: dict[str, Any],
    *,
    set_parts: list[str],
    values: dict[str, dict[str, Any]],
) -> None:
    """Persist ``spec_slug`` + ``spec_s3_prefix`` if the payload carries them.

    Both come on the architect's ``SPEC.READY`` event. The state-router
    needs them on the STATE row for downstream dispatches: the Critic
    payload, the spec-PR open call, and every Implementer / Reviewer /
    Tester invocation that resolves spec docs out of S3.
    """
    spec_slug = payload.get("spec_slug")
    if isinstance(spec_slug, str) and spec_slug:
        set_parts.append("spec_slug = :spec")
        values[":spec"] = {"S": spec_slug}
    spec_s3_prefix = payload.get("spec_s3_prefix")
    if isinstance(spec_s3_prefix, str) and spec_s3_prefix:
        set_parts.append("spec_s3_prefix = :spec_prefix")
        values[":spec_prefix"] = {"S": spec_s3_prefix}


def accumulate_issue_triaged(
    payload: dict[str, Any],
    *,
    set_parts: list[str],
    values: dict[str, dict[str, Any]],
) -> None:
    """Persist ``workflow_kind`` + ``triage_action`` from ISSUE.TRIAGED.

    The router's ``handle_triage_decided`` branches first on the action
    (``proceed`` / ``ask`` / ``defer`` / ``decline``), then on
    ``workflow_kind`` for the proceed case. Without these projections
    every issue-driven run falls into the ``spec_driven`` proceed path.
    """
    workflow_kind = payload.get("workflow_kind")
    if isinstance(workflow_kind, str) and workflow_kind:
        set_parts.append("workflow_kind = :wk")
        values[":wk"] = {"S": workflow_kind}
    action = payload.get("action")
    if isinstance(action, str) and action:
        set_parts.append("triage_action = :ta")
        values[":ta"] = {"S": action}


def accumulate_spec_ready(
    payload: dict[str, Any],
    *,
    set_parts: list[str],
    values: dict[str, dict[str, Any]],
) -> None:
    """Persist ``tasks_total`` and ``task_ids`` from a SPEC.READY payload.

    The state-router's ``handle_spec_approved`` reads ``task_ids`` off the
    STATE row to seed pending TASK rows; without this projection the
    seeder has no list to walk and the run hangs in ``tasks_in_progress``
    with zero tasks.
    """
    if isinstance(payload.get("task_count"), int):
        set_parts.append("tasks_total = :tt")
        values[":tt"] = {"N": str(payload["task_count"])}
    task_ids = payload.get("task_ids")
    if isinstance(task_ids, list) and task_ids and all(isinstance(t, str) for t in task_ids):
        set_parts.append("task_ids = :tids")
        values[":tids"] = {"SS": list(task_ids)}


def accumulate_usage(
    payload: dict[str, Any],
    *,
    add_parts: list[str],
    values: dict[str, dict[str, Any]],
) -> None:
    """Append ADD clauses for any per-event usage fields the payload carries.

    Agent ``*.READY`` payloads (SpecReady, CritiqueReady, TaskReady,
    ReviewReady, TestReportReady) inherit from
    :class:`common.events.UsagePayload`; values default to zero when an
    agent hasn't been wired yet, and we skip the ADD for zero values to
    avoid a no-op DDB write that still costs WCUs.
    """
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


# ---------------------------------------------------------------------------
# State-transition application (flag-gated; Phase 2 cutover)
# ---------------------------------------------------------------------------

# Events that advance task-level state. Anything else is treated as
# run-level for transition purposes.
TASK_LEVEL_EVENTS = frozenset(
    {
        "TASK.READY",
        "TASK.APPROVED",
        "TASK.REJECTED",
        "TASK.ITERATION_REQUESTED",
    },
)


@tracer.capture_method
def apply_state_transition(
    *,
    run_id: str,
    event_type: str,
    envelope: dict[str, Any],
) -> None:
    """Compute the next state for ``event_type`` and apply it conditionally.

    Run-level events update the STATE row's ``current_state``; task-level
    events update the corresponding TASK row's ``status``. Idempotent
    via DDB ``ConditionExpression`` on the previous state value.
    """
    if event_type in TASK_LEVEL_EVENTS:
        apply_task_state_transition(run_id=run_id, event_type=event_type, envelope=envelope)
    else:
        apply_run_state_transition(run_id=run_id, event_type=event_type, envelope=envelope)


def apply_run_state_transition(
    *,
    run_id: str,
    event_type: str,
    envelope: dict[str, Any],
) -> None:
    """Read run STATE, compute next state, write conditionally."""
    current = read_run_state(run_id)
    next_state = apply_run_transition(
        event_type=cast("PlatformEventType", event_type),
        current_state=current,
    )
    if next_state is None:
        return
    event_id = envelope.get("event_id", "")
    timestamp = envelope.get("timestamp", "")
    advance_run_state(
        run_id=run_id,
        from_state=current,
        to_state=next_state,
        event_id=event_id,
        timestamp=timestamp,
    )


ITERATION_ACCUMULATOR_STATES = frozenset(
    {TaskState.iterating, TaskState.implementer_running},
)
"""States in which a fresh ``TASK.ITERATION_REQUESTED`` is queued, not advanced.

A user can post a second ``/aidlc fix X`` while the implementer is still
mid-flight on the first. There's no state transition for those moments
(no ``iterating → iterating``), so the projector accumulates the new
feedback onto the task row in place and lets the in-flight iteration
finish; whichever iteration flushes ``pending_feedback`` consumes it.
"""


def apply_task_state_transition(
    *,
    run_id: str,
    event_type: str,
    envelope: dict[str, Any],
) -> None:
    """Read TASK row, compute next state, write conditionally.

    For ``TASK.ITERATION_REQUESTED``, also append the delivery_id to the
    task's ``delivery_ids`` set and the feedback item to
    ``pending_feedback``. The state advance + accumulator update happen
    in one ``UpdateItem`` so they're atomic.

    When the event arrives mid-iteration (current state is
    :data:`ITERATION_ACCUMULATOR_STATES`), there is no state transition
    but the new feedback still has to land — :func:`accumulate_iteration_in_place`
    writes the accumulators with a state-guard but no advance.
    """
    payload = envelope.get("payload") or {}
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        logger.warning("task event missing task_id", extra={"event_type": event_type})
        return
    current = read_task_state(run_id, task_id)
    if current is None:
        logger.info(
            "task row missing — projector skipping transition",
            extra={"run_id": run_id, "task_id": task_id, "event_type": event_type},
        )
        return
    next_state = apply_task_transition(
        event_type=cast("PlatformEventType", event_type),
        current_state=current,
    )
    if next_state is None:
        if event_type == "TASK.ITERATION_REQUESTED" and current in ITERATION_ACCUMULATOR_STATES:
            accumulate_iteration_in_place(
                run_id=run_id,
                task_id=task_id,
                current_state=current,
                event_id=envelope.get("event_id", ""),
                timestamp=envelope.get("timestamp", ""),
                payload=payload,
            )
        return
    advance_task_state(
        run_id=run_id,
        task_id=task_id,
        from_state=current,
        to_state=next_state,
        event_id=envelope.get("event_id", ""),
        timestamp=envelope.get("timestamp", ""),
        payload=payload,
        event_type=event_type,
    )


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


def advance_run_state(
    *,
    run_id: str,
    from_state: RunState | None,
    to_state: RunState,
    event_id: str,
    timestamp: str,
) -> None:
    """Conditional UpdateItem that advances ``current_state``."""
    values: dict[str, dict[str, str | int]] = {
        ":to": {"S": to_state.value},
        ":eid": {"S": event_id},
        ":ts": {"S": timestamp},
        ":one": {"N": "1"},
    }
    if from_state is None:
        condition = "attribute_not_exists(current_state)"
    else:
        condition = "current_state = :from"
        values[":from"] = {"S": from_state.value}
    try:
        ddb().update_item(
            TableName=runs_table(),
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
            UpdateExpression=(
                "SET current_state = :to, last_event_id = :eid, last_event_at = :ts "
                "ADD state_transitions :one"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(
                "run state already advanced (idempotent no-op)",
                extra={"run_id": run_id, "to": to_state.value},
            )
            return
        raise


def advance_task_state(
    *,
    run_id: str,
    task_id: str,
    from_state: TaskState,
    to_state: TaskState,
    event_id: str,
    timestamp: str,
    payload: dict[str, Any],
    event_type: str,
) -> None:
    """Conditional UpdateItem that advances ``status`` on a TASK row.

    For ``TASK.ITERATION_REQUESTED``, also accumulates the delivery_id
    set + pending_feedback list in the same call.
    """
    set_parts = [
        "#s = :to",
        "last_event_id = :eid",
        "last_event_at = :ts",
    ]
    add_parts: list[str] = []
    remove_parts: list[str] = []
    values: dict[str, Any] = {
        ":from": {"S": from_state.value},
        ":to": {"S": to_state.value},
        ":eid": {"S": event_id},
        ":ts": {"S": timestamp},
    }
    names = {"#s": "status"}
    apply_iteration_request_clauses(
        event_type=event_type,
        payload=payload,
        set_parts=set_parts,
        add_parts=add_parts,
        values=values,
    )
    apply_task_ready_clauses(
        event_type=event_type,
        from_state=from_state,
        set_parts=set_parts,
        remove_parts=remove_parts,
        values=values,
    )
    pr_url = payload.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        set_parts.append("pr_url = :pr_url")
        values[":pr_url"] = {"S": pr_url}
    expression = "SET " + ", ".join(set_parts)
    if add_parts:
        expression += " ADD " + ", ".join(add_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)
    try:
        ddb().update_item(
            TableName=runs_table(),
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": f"TASK#{task_id}"}},
            UpdateExpression=expression,
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(
                "task state already advanced (idempotent no-op)",
                extra={"run_id": run_id, "task_id": task_id, "to": to_state.value},
            )
            return
        raise


def apply_iteration_request_clauses(
    *,
    event_type: str,
    payload: dict[str, Any],
    set_parts: list[str],
    add_parts: list[str],
    values: dict[str, Any],
) -> None:
    """Append feedback + delivery_id clauses for ``TASK.ITERATION_REQUESTED``.

    No-op for any other event type. Mutates the caller's expression
    builders in place.
    """
    if event_type != "TASK.ITERATION_REQUESTED":
        return
    accumulate_iteration_data(payload=payload, set_parts=set_parts, values=values)
    delivery_id = payload.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        add_parts.append("delivery_ids :did")
        values[":did"] = {"SS": [delivery_id]}


def apply_task_ready_clauses(
    *,
    event_type: str,
    from_state: TaskState,
    set_parts: list[str],
    remove_parts: list[str],
    values: dict[str, Any],
) -> None:
    """Flush the iteration queue when ``TASK.READY`` arrives from ``iterating``.

    Iteration N's fix commit just landed; clear ``pending_feedback`` and
    ``delivery_ids`` so iteration N+1 starts clean instead of
    re-processing items the implementer already addressed.
    ``delivery_ids`` is ``REMOVE``'d because DDB doesn't allow empty
    string sets.
    """
    if event_type != "TASK.READY" or from_state != TaskState.iterating:
        return
    set_parts.append("pending_feedback = :empty_list")
    values[":empty_list"] = {"L": []}
    remove_parts.append("delivery_ids")


def accumulate_iteration_in_place(
    *,
    run_id: str,
    task_id: str,
    current_state: TaskState,
    event_id: str,
    timestamp: str,
    payload: dict[str, Any],
) -> None:
    """Append iteration feedback + delivery_id without advancing task state.

    Called when ``TASK.ITERATION_REQUESTED`` arrives while the task is
    in ``iterating`` / ``implementer_running``. The conditional update
    still guards on the current state so we don't clobber a task that
    just transitioned (e.g., to ``pr_open`` from a concurrent
    ``TASK.READY``). On a lost race, the feedback is dropped — the next
    iteration request will queue properly once state stabilises.
    """
    set_parts = ["last_event_id = :eid", "last_event_at = :ts"]
    add_parts: list[str] = []
    values: dict[str, Any] = {
        ":from": {"S": current_state.value},
        ":eid": {"S": event_id},
        ":ts": {"S": timestamp},
    }
    names = {"#s": "status"}
    accumulate_iteration_data(payload=payload, set_parts=set_parts, values=values)
    delivery_id = payload.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        add_parts.append("delivery_ids :did")
        values[":did"] = {"SS": [delivery_id]}
    expression = "SET " + ", ".join(set_parts)
    if add_parts:
        expression += " ADD " + ", ".join(add_parts)
    try:
        ddb().update_item(
            TableName=runs_table(),
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": f"TASK#{task_id}"}},
            UpdateExpression=expression,
            ConditionExpression="#s = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(
                "task moved while accumulating iteration feedback",
                extra={"run_id": run_id, "task_id": task_id, "current": current_state.value},
            )
            return
        raise


def accumulate_iteration_data(
    *,
    payload: dict[str, Any],
    set_parts: list[str],
    values: dict[str, Any],
) -> None:
    """Append the iteration's feedback to ``pending_feedback`` on the task row.

    DDB doesn't support set-of-maps, so feedback is a List of Maps. The
    update uses ``list_append(if_not_exists(pending_feedback, :empty), :item)``
    so first delivery seeds the list and subsequent ones extend it.
    """
    feedback = payload.get("feedback")
    if not isinstance(feedback, dict):
        return
    set_parts.append(
        "pending_feedback = list_append("
        "if_not_exists(pending_feedback, :empty_list), :feedback_one"
        ")",
    )
    values[":empty_list"] = {"L": []}
    values[":feedback_one"] = {"L": [{"M": map_feedback(feedback)}]}


def map_feedback(feedback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert a flat feedback dict into a one-level DDB map.

    All FeedbackItem fields are scalars (str / int / bool); nested
    structures don't exist today. If a future FeedbackItem grows nested
    state, extend this function.
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


# ---------------------------------------------------------------------------
# AgentCore Memory pass-through (unchanged)
# ---------------------------------------------------------------------------


@tracer.capture_method
def forward_to_memory(envelope: dict[str, Any]) -> None:
    """Emit the envelope to AgentCore Memory as a CreateEvent."""
    actor_id = envelope.get("payload", {}).get("project_slug") or envelope.get("actor_id", "system")
    session_id = envelope.get("run_id", "system")
    try:
        agentcore().create_event(
            memoryId=memory_id(),
            actorId=actor_id,
            sessionId=session_id,
            payload=[
                {
                    "blob": json.dumps(envelope).encode("utf-8"),
                    "contentType": "application/json",
                },
            ],
        )
    except Exception as exc:
        logger.warning("memory CreateEvent failed", extra={"err": repr(exc)})
        metrics.add_metric(name="MemoryWriteFailures", unit=MetricUnit.Count, value=1)
