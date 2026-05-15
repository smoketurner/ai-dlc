"""Projector Lambda — fans out platform events into the read model + memory.

Triggered by every event on the platform EventBridge bus. The
projector's job is small and uniform:

* Insert the EVENT timeline row (``sk=EVENT#{event_id}``) with
  ``attribute_not_exists(sk)`` — this row is the master idempotency
  key. A re-delivered envelope fails the condition and the entire
  transaction rolls back.
* Update the SUMMARY row (``sk=SUMMARY``) with accumulators and
  metadata only — token / cost / duration totals, GSI keys, PR URL.
  No state cursor, no decision logic.

Because the transaction is atomic, re-delivery is a complete no-op
— no double-counted usage totals, no duplicate memory writes. The
DDB Stream's INSERT record for the EVENT row is the wake-up signal:
the EventBridge Pipe forwards it to the state-router's SQS queue.

After a successful commit, the projector forwards the envelope to
AgentCore Memory via ``CreateEvent``. Memory writes are gated on the
DDB transaction succeeding, so they're also idempotent on event_id.

The state machine is gone. All decision logic now lives in the
state-router's ``decide(events)`` pure function, which queries the
event log directly. The projector is intentionally dumb.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError

from common.ddb import PutBuilder, TransactWriteItemsBuilder, UpdateBuilder
from common.events import UntypedEnvelope

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="event_projector")
tracer = Tracer(service="event_projector")
metrics = Metrics(namespace="ai-dlc", service="event_projector")


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
    """Project one EventBridge event into the read model + memory."""
    if "detail" not in event or "detail-type" not in event:
        logger.warning("unknown trigger shape", extra={"keys": sorted(event.keys())})
        return {"ok": False, "error": "unknown trigger"}
    return handle_eventbridge(event)


def handle_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Single EventBridge invocation — ``event['detail']`` is the envelope."""
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


@tracer.capture_method
def project_event(*, envelope: UntypedEnvelope, detail: dict[str, Any]) -> bool:
    """Insert the EVENT row + accumulator update on SUMMARY in one transaction.

    Returns ``True`` when the transaction committed, ``False`` on a
    conditional-check loss (re-delivery via the EVENT row). On
    ``False`` the caller skips the AgentCore Memory write so memory
    is also idempotent on event_id.
    """
    run_id = str(envelope.run_id)
    event_type = envelope.type or "UNKNOWN"
    transaction = TransactWriteItemsBuilder()
    transaction.put(event_row_item(run_id, event_type, detail))
    transaction.update(summary_row_item(run_id, event_type, detail))
    committed = transaction.commit(ddb())
    if not committed:
        logger.info(
            "event already projected (idempotent no-op)",
            extra={"run_id": run_id, "event_type": event_type},
        )
    return committed


def event_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> PutBuilder:
    """The EVENT timeline row — master idempotency key for this event.

    The DDB Stream's INSERT record on this row is what the
    EventBridge Pipe forwards to the state-router beacon queue. The
    ``run_id`` attribute lets the pipe's input template build the SQS
    message body without parsing ``pk``.
    """
    event_id = detail.get("event_id", "unknown")
    payload = detail.get("payload") or {}
    project_slug = payload.get("project_slug") or run_id
    return PutBuilder(
        table=runs_table(),
        item={
            "pk": f"RUN#{run_id}",
            "sk": f"EVENT#{event_id}",
            "type": event_type,
            "envelope": json.dumps(detail),
            "run_id": run_id,
            "project_slug": project_slug,
        },
    ).condition_not_exists("sk")


def summary_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> UpdateBuilder:
    """Build the SUMMARY row update.

    Carries:

    * ``status`` — latest event type (for dashboard listing).
    * ``updated_at`` / ``last_event_id`` / ``last_event_at``.
    * Always-on metadata via ``if_not_exists``: ``project_slug``,
      ``created_at``, source-issue fields, GSI keys.
    * Per-event projections: ``pr_url`` + ``gsi_pr`` on
      ``IMPL_PR.OPENED`` so the webhook PR lookup keeps working.
    * Token / cost / duration accumulators (always ADD non-zero).
    """
    payload = detail.get("payload") or {}
    timestamp = detail.get("timestamp") or now_iso()
    event_id = detail.get("event_id") or ""
    update = (
        UpdateBuilder(
            table=runs_table(),
            key={"pk": f"RUN#{run_id}", "sk": "SUMMARY"},
        )
        .set("status", event_type)
        .set("updated_at", timestamp)
        .set("last_event_id", event_id)
        .set("last_event_at", timestamp)
        .set_if_not_exists("run_id", run_id)
        .set_if_not_exists("created_at", timestamp)
    )
    apply_metadata_projections(update, run_id=run_id, event_type=event_type, payload=payload)
    apply_usage_totals(update, payload=payload)
    return update


def apply_metadata_projections(
    update: UpdateBuilder,
    *,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Project metadata fields onto the SUMMARY row.

    Always-on (set_if_not_exists): ``project_slug``,
    ``source_issue_url`` + ``gsi1pk`` / ``gsi1sk`` (for issue
    lookup), ``source_issue_title``, ``source_issue_body``,
    ``requestor``, ``target_repo``, ``intent``. These are all
    invariant for the lifetime of a run.

    Per-event: ``pr_url`` + ``gsi_pr`` on ``IMPL_PR.OPENED`` (PR
    lookup index for webhooks).
    """
    set_if_truthy(update, "project_slug", payload.get("project_slug"), if_not_exists=True)
    set_if_truthy(update, "requestor", payload.get("requestor"), if_not_exists=True)
    set_if_truthy(update, "target_repo", payload.get("target_repo"), if_not_exists=True)
    set_if_truthy(update, "intent", payload.get("intent"), if_not_exists=True)
    source_issue_url = payload.get("source_issue_url")
    if isinstance(source_issue_url, str) and source_issue_url:
        update.set_if_not_exists("gsi1pk", f"ISSUE#{source_issue_url}")
        update.set_if_not_exists("gsi1sk", f"RUN#{run_id}")
        update.set_if_not_exists("source_issue_url", source_issue_url)
    set_if_truthy(
        update,
        "source_issue_title",
        payload.get("source_issue_title"),
        if_not_exists=True,
    )
    set_if_truthy(
        update,
        "source_issue_body",
        payload.get("source_issue_body"),
        if_not_exists=True,
    )
    if event_type == "IMPL_PR.OPENED":
        pr_url = payload.get("pr_url")
        if isinstance(pr_url, str) and pr_url:
            update.set("pr_url", pr_url)
            update.set("gsi_pr", f"PR#{pr_url}")


def set_if_truthy(
    update: UpdateBuilder,
    attribute: str,
    value: Any,
    *,
    if_not_exists: bool = False,
) -> None:
    """SET an attribute when ``value`` is a non-empty string."""
    if not isinstance(value, str) or not value:
        return
    if if_not_exists:
        update.set_if_not_exists(attribute, value)
    else:
        update.set(attribute, value)


def apply_usage_totals(update: UpdateBuilder, *, payload: dict[str, Any]) -> None:
    """ADD per-event token / cost / duration totals when non-zero."""
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


def now_iso() -> str:
    """Tz-aware UTC ISO timestamp (used when an envelope omits one)."""
    return datetime.now(UTC).isoformat()


@tracer.capture_method
def forward_to_memory(envelope: dict[str, Any]) -> None:
    """Emit the envelope to AgentCore Memory as a CreateEvent.

    Only invoked after the projector's transaction commits, so memory
    writes are idempotent on event_id.
    """
    actor_id = envelope.get("payload", {}).get("project_slug") or envelope.get(
        "actor_id",
        "system",
    )
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
