"""Eval aggregator Lambda — schedule-driven roll-up of PR efficiency metrics.

Triggered by an EventBridge schedule (hourly). Pulls the last 7 days
of :class:`PRTelemetry` rows from the telemetry DDB table and the last
30 days of :class:`ClassifiedComment` records from S3, computes
per-bucket :class:`EfficiencyMetrics`, and emits a ``DriftSignal``
event whenever the C4 thresholds trigger (≥20% friction delta vs the
30-day baseline AND ≥10 PRs in the rolling window).

The aggregator is read-mostly: it reads from DDB + S3, writes a single
aggregate JSON snapshot to S3, and emits zero or more events to
EventBridge. It does not mutate the source rows. This keeps the
function safely idempotent on retry — late-window rows just get
re-counted, never double-counted into a state.

The Lambda is deliberately thin glue around the pure-function helpers
in :mod:`eval_aggregator.aggregate` — that module is the unit-test
target.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from common.eval import (
    AgentOwner,
    ClassifiedComment,
    CommentCategory,
    DriftSignal,
    EfficiencyMetrics,
    PRTelemetry,
)
from common.events import EventEnvelope
from common.ids import new_correlation_id, new_event_id, new_run_id
from eval_aggregator.aggregate import (
    BucketKey,
    aggregate,
    bucket_key,
    dominant_category,
    drift_delta_pct,
    drift_detected,
    weighted_friction_score,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="eval_aggregator")

ROLLING_DAYS = 7
BASELINE_DAYS = 30


@cache
def s3() -> S3Client:
    return boto3.client("s3")


@cache
def ddb() -> DynamoDBClient:
    return boto3.client("dynamodb")


@cache
def events_client() -> EventBridgeClient:
    return boto3.client("events", region_name=os.environ["AWS_REGION"])


def telemetry_table() -> str:
    return os.environ["AIDLC_PR_TELEMETRY_TABLE"]


def artifacts_bucket() -> str:
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def bus_name() -> str:
    return os.environ["AIDLC_BUS_NAME"]


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Run one aggregation pass. ``event`` is unused on the schedule trigger."""
    del event
    now = datetime.now(tz=UTC)
    rolling_start = now - timedelta(days=ROLLING_DAYS)
    baseline_start = now - timedelta(days=BASELINE_DAYS)

    rolling_rows = load_telemetry(start=rolling_start, end=now)
    baseline_rows = load_telemetry(start=baseline_start, end=rolling_start)
    comments = load_comments(start=rolling_start)

    rolling = aggregate(
        rolling_rows,
        comments=comments,
        window_start=rolling_start,
        window_end=now,
    )
    baseline = aggregate(
        baseline_rows,
        comments={},  # baseline window only used for the friction-score comparison
        window_start=baseline_start,
        window_end=rolling_start,
    )

    persist_snapshot(now=now, rolling=rolling, baseline=baseline)
    drifts = detect_drift(rolling=rolling, baseline=baseline, comments=comments)
    publish(drifts)

    return {
        "ok": True,
        "rolling_buckets": len(rolling),
        "drift_signals": len(drifts),
    }


def load_telemetry(*, start: datetime, end: datetime) -> list[PRTelemetry]:
    """Scan the telemetry table for rows whose ``opened_at`` is in ``[start, end)``."""
    rows: list[PRTelemetry] = []
    paginator = ddb().get_paginator("scan")
    for page in paginator.paginate(TableName=telemetry_table()):
        for item in page.get("Items", []):
            row = item_to_telemetry(item)
            if row is None:
                continue
            if start <= row.opened_at < end:
                rows.append(row)
    return rows


def item_to_telemetry(item: dict[str, Any]) -> PRTelemetry | None:
    """Turn a DDB item into a :class:`PRTelemetry`, or ``None`` if invalid."""
    try:
        return PRTelemetry.model_validate(decode_ddb_item(item))
    except ValidationError as exc:
        logger.warning("skipping malformed telemetry row", extra={"err": str(exc)})
        return None


def decode_ddb_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a low-level DynamoDB item to a plain dict.

    Handles only the attribute types the telemetry table actually uses
    (``S``, ``N``, ``BOOL``, ``NULL``). Datetimes are passed through as
    ISO-8601 strings; Pydantic parses them into ``datetime`` on validate.
    """
    out: dict[str, Any] = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            out[k] = int(v["N"]) if "." not in v["N"] else float(v["N"])
        elif "BOOL" in v:
            out[k] = v["BOOL"]
        elif "NULL" in v:
            out[k] = None
    return out


def load_comments(*, start: datetime) -> dict[BucketKey, dict[CommentCategory, int]]:
    """List classified-comment objects in S3 from ``start`` forward; bucket them.

    The keys are sliced by date prefix (one day per directory) so the
    listing cost scales with the window size, not the lifetime archive.
    Each object holds a single :class:`ClassifiedComment` JSON document.

    Bucketing requires associating each comment with its PR's
    ``(target_repo, agent_owner, prompt_variant)`` — that mapping comes
    from the same telemetry rows the rolling window pulls. The handler
    threads the mapping by re-loading telemetry once and indexing it.
    """
    by_pr: dict[str, BucketKey] = {
        row.pr_url: bucket_key(row)
        for row in load_telemetry(start=start, end=datetime.now(tz=UTC))
    }
    counts: dict[BucketKey, dict[CommentCategory, int]] = {}
    bucket = artifacts_bucket()
    paginator = s3().get_paginator("list_objects_v2")
    cursor = start
    end = datetime.now(tz=UTC)
    while cursor < end:
        prefix = cursor.strftime("evals/classified_comments/%Y-%m-%d/")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                comment = read_classified_comment(bucket, obj["Key"])
                if comment is None:
                    continue
                key = by_pr.get(comment.pr_url)
                if key is None:
                    continue
                inner = counts.setdefault(key, {})
                inner[comment.category] = inner.get(comment.category, 0) + 1
        cursor += timedelta(days=1)
    return counts


def read_classified_comment(bucket: str, key: str) -> ClassifiedComment | None:
    """Read one comment-classification JSON object from S3."""
    try:
        body = s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        return ClassifiedComment.model_validate_json(body)
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning("skipping malformed comment", extra={"key": key, "err": str(exc)})
        return None


def persist_snapshot(
    *,
    now: datetime,
    rolling: list[EfficiencyMetrics],
    baseline: list[EfficiencyMetrics],
) -> str:
    """Write the aggregate snapshot to ``s3://artifacts/evals/efficiency/{ts}.json``."""
    body = json.dumps(
        {
            "generated_at": now.isoformat(),
            "rolling": [m.model_dump(mode="json") for m in rolling],
            "baseline": [m.model_dump(mode="json") for m in baseline],
        },
        default=str,
    )
    key = f"evals/efficiency/{now.strftime('%Y-%m-%dT%H')}.json"
    s3().put_object(
        Bucket=artifacts_bucket(),
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    return key


def detect_drift(
    *,
    rolling: list[EfficiencyMetrics],
    baseline: list[EfficiencyMetrics],
    comments: dict[BucketKey, dict[CommentCategory, int]],
) -> list[DriftSignal]:
    """For each rolling bucket, compare against the matching baseline bucket."""
    baseline_by_key: dict[tuple[str, AgentOwner, str], EfficiencyMetrics] = {
        (m.target_repo, m.agent_owner, m.prompt_variant): m for m in baseline
    }
    out: list[DriftSignal] = []
    for m in rolling:
        key = (m.target_repo, m.agent_owner, m.prompt_variant)
        base = baseline_by_key.get(key)
        baseline_score = base.weighted_friction_score if base is not None else 0.0
        if not drift_detected(
            rolling_score=m.weighted_friction_score,
            baseline_score=baseline_score,
            sample_size=m.pr_count,
        ):
            continue
        out.append(
            DriftSignal(
                target_repo=m.target_repo,
                agent_owner=m.agent_owner,
                prompt_variant=m.prompt_variant,
                detected_at=m.window_end,
                rolling_window_score=m.weighted_friction_score,
                baseline_score=baseline_score,
                delta_pct=drift_delta_pct(
                    rolling_score=m.weighted_friction_score,
                    baseline_score=baseline_score,
                ),
                sample_size=m.pr_count,
                dominant_category=dominant_category(comments.get(key, {})),
            ),
        )
    return out


def publish(drifts: list[DriftSignal]) -> None:
    """Emit one ``EVAL.DRIFT_DETECTED`` envelope per drift bucket."""
    for d in drifts:
        envelope = EventEnvelope[DriftSignal](
            event_id=new_event_id(),
            type="EVAL.DRIFT_DETECTED",
            run_id=new_run_id(),
            correlation_id=new_correlation_id(),
            actor_id="eval_aggregator",
            payload=d,
        )
        events_client().put_events(
            Entries=[
                {
                    "Source": "ai-dlc.eval_aggregator",
                    "DetailType": "EVAL.DRIFT_DETECTED",
                    "Detail": envelope.model_dump_json(),
                    "EventBusName": bus_name(),
                },
            ],
        )


# Re-export for tests.
__all__ = [
    "BASELINE_DAYS",
    "ROLLING_DAYS",
    "aggregate",
    "drift_detected",
    "handler",
    "weighted_friction_score",
]
