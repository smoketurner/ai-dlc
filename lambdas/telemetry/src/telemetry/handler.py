"""Telemetry Lambda — categorize rejections and persist labeled records.

Triggered by an EventBridge rule on ``SPEC.REJECTED`` and ``TASK.REJECTED``.
For each rejection we:

1. Pull the rejection reason from the event payload.
2. Ask Bedrock Haiku 4.5 to label it against the fixed taxonomy in
   :data:`CATEGORIES`. The model returns one category id; if it returns
   anything else we fall back to ``other`` and log the raw response.
3. Write the labeled record to S3 at
   ``evals/rejections/{date}/{run_id}/{gate_ref}.json`` so downstream
   consumers (eval runner, improvement proposer) can read it.
4. Increment per-run + per-project rolling counters on the runs table so
   the dashboard can surface category histograms without re-reading S3.

Categorization runs on a small, cheap model (Haiku) — the categorization
result is advisory, never gates the pipeline. We deliberately use a closed
taxonomy so the proposer has a stable feature space to optimise against.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext

from common.events import IncomingEnvelope, SpecRejected, TaskRejected

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client

RejectionEnvelope = IncomingEnvelope[SpecRejected | TaskRejected]

logger = Logger(service="telemetry")
tracer = Tracer(service="telemetry")
metrics = Metrics(namespace="ai-dlc", service="telemetry")

CATEGORIES = (
    "missing-acceptance-criteria",
    "unclear-requirement",
    "spec-too-large",
    "lint-failed",
    "test-failed",
    "style-violation",
    "convention-violation",
    "external-dep-issue",
    "out-of-scope-edit",
    "other",
)

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode an EventBridge ``detail`` JSON string into a dict if needed.

    EventBridge usually delivers ``detail`` as a dict to Lambda, but some
    routes (e.g. archive/replay) wrap it as a JSON string. ``parse()``
    against ``EventBridgeEnvelope`` requires a dict.
    """
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


@cache
def s3() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def bedrock() -> Any:
    """Process-cached Bedrock runtime client."""
    return boto3.client("bedrock-runtime")


def artifacts_bucket() -> str:
    """Bucket holding labeled rejection records under ``evals/rejections/``."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def runs_table() -> str:
    """Runs read-model table; gets categorized counters merged into STATE rows."""
    return os.environ["AIDLC_RUNS_TABLE"]


def model_id() -> str:
    """Categorization model — overridable via ``AIDLC_TELEMETRY_MODEL_ID``."""
    return os.environ.get("AIDLC_TELEMETRY_MODEL_ID", DEFAULT_MODEL_ID)


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Categorize one rejection event."""
    try:
        # parse() is typed to allow batch shapes; EventBridgeEnvelope always
        # returns the single inner model, hence the cast.
        envelope = cast(
            "RejectionEnvelope",
            parse(
                event=normalise(event),
                model=RejectionEnvelope,
                envelope=EventBridgeEnvelope,
            ),
        )
    except ValidationError as exc:
        logger.warning("invalid event", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}

    if envelope.type not in {"SPEC.REJECTED", "TASK.REJECTED"}:
        logger.info("ignoring non-rejection", extra={"type": envelope.type})
        return {"ok": True, "ignored": True}
    payload = envelope.payload
    run_id = str(envelope.run_id)
    gate_ref = derive_gate_ref(envelope.type, payload)
    reason = payload.reason
    category = classify(reason, event_type=envelope.type, payload=payload)
    record = build_record(
        envelope=envelope, gate_ref=gate_ref, category=category
    )
    persist_record(run_id=run_id, gate_ref=gate_ref, record=record)
    update_counters(run_id=run_id, project_slug=payload.project_slug, category=category)
    logger.info(
        "rejection categorized",
        run_id=run_id,
        gate_ref=gate_ref,
        category=category,
    )
    return {"ok": True, "category": category, "run_id": run_id, "gate_ref": gate_ref}


def derive_gate_ref(event_type: str, payload: SpecRejected | TaskRejected) -> str:
    """Translate the event type + payload into the conventional gate_ref."""
    if event_type == "SPEC.REJECTED":
        return "spec"
    task_id = payload.task_id if isinstance(payload, TaskRejected) else "unknown"
    return f"task:{task_id}"


def classify(
    reason: str,
    *,
    event_type: str,
    payload: SpecRejected | TaskRejected,
) -> str:
    """Ask Haiku to map ``reason`` to one of :data:`CATEGORIES`."""
    if not reason.strip():
        return "other"
    prompt = build_prompt(reason=reason, event_type=event_type, payload=payload)
    try:
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 32,
                "temperature": 0.0,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp = bedrock().invoke_model(modelId=model_id(), body=body)
        body_bytes = resp["body"].read()
        parsed = json.loads(body_bytes)
        text_blocks = parsed.get("content") or []
        raw_label = ""
        for block in text_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                raw_label = (block.get("text") or "").strip().lower()
                break
        if raw_label in CATEGORIES:
            return raw_label
        logger.warning("unknown category from model", extra={"raw": raw_label})
    except Exception as exc:
        logger.warning("classify failed", extra={"err": repr(exc)})
    return "other"


SYSTEM_PROMPT = """\
You categorise SDLC rejection reasons into exactly one of these labels:

- missing-acceptance-criteria
- unclear-requirement
- spec-too-large
- lint-failed
- test-failed
- style-violation
- convention-violation
- external-dep-issue
- out-of-scope-edit
- other

Reply with the label only. No prose, no punctuation, no quotes.
"""


def build_prompt(
    *,
    reason: str,
    event_type: str,
    payload: SpecRejected | TaskRejected,
) -> str:
    """Compose the user prompt for the categorization call."""
    parts = [f"Event: {event_type}", f"Spec: {payload.spec_slug}"]
    if isinstance(payload, TaskRejected):
        parts.append(f"Task: {payload.task_id}")
    parts += ["", "Reviewer reason:", reason.strip()]
    return "\n".join(parts)


def build_record(
    *,
    envelope: RejectionEnvelope,
    gate_ref: str,
    category: str,
) -> dict[str, Any]:
    """Assemble the labeled record persisted to S3."""
    return {
        "schema_version": "1.0",
        "labeled_at": datetime.now(UTC).isoformat(),
        "event_type": envelope.type,
        "gate_ref": gate_ref,
        "category": category,
        "envelope": envelope.model_dump(mode="json"),
    }


def persist_record(*, run_id: str, gate_ref: str, record: dict[str, Any]) -> None:
    """Write the labeled record to S3 under ``evals/rejections/...``."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    safe_gate = gate_ref.replace(":", "_")
    key = f"evals/rejections/{date}/{run_id}/{safe_gate}.json"
    s3().put_object(
        Bucket=artifacts_bucket(),
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def update_counters(*, run_id: str, project_slug: str, category: str) -> None:
    """Increment per-run + per-project rolling counters on the runs table."""
    counter_attr = f"category_{category.replace('-', '_')}"
    ddb().update_item(
        TableName=runs_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
        UpdateExpression="ADD #c :one, total_rejections :one",
        ExpressionAttributeNames={"#c": counter_attr},
        ExpressionAttributeValues={":one": {"N": "1"}},
    )
    month = datetime.now(UTC).strftime("%Y-%m")
    ddb().update_item(
        TableName=runs_table(),
        Key={
            "pk": {"S": f"PROJECT#{project_slug}"},
            "sk": {"S": f"REJECTIONS#{month}"},
        },
        UpdateExpression="ADD #c :one",
        ExpressionAttributeNames={"#c": counter_attr},
        ExpressionAttributeValues={":one": {"N": "1"}},
    )
