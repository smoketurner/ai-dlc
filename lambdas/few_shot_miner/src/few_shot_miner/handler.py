"""Few-shot miner — capture successful runs for downstream prompt optimization.

Triggered by the runs-table DynamoDB stream. We act on a STATE-row update
that transitions ``status`` to ``RUN.COMPLETED`` *and* whose
``total_rejections`` is zero (clean run, no human pushback). For each
qualifying run we mine two kinds of few-shot examples:

  * ``intent_to_spec`` — the architect's input (project_slug + intent)
    paired with the produced spec bundle. One example per run.
  * ``task_to_diff``   — the implementer's input (spec context + task_id)
    paired with the resulting PR diff summary. One example per task.

Examples land at:

    s3://artifacts/evals/few-shots/{kind}/{date}/{run_id}/{ix}.json

The proposer reads these as a few-shot bank when proposing prompt
updates. We do *not* mine rejected runs here — that's the telemetry
agent's job; rejection records carry the labeling. Mining only clean
runs keeps the example bank high-signal.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="few_shot_miner")


@cache
def s3() -> S3Client:
    """Process-cached S3 client."""
    return boto3.client("s3")


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


def artifacts_bucket() -> str:
    """Bucket where few-shot examples and the spec bundles already live."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def runs_table() -> str:
    """Runs read-model table — used to pull the run's event timeline."""
    return os.environ["AIDLC_RUNS_TABLE"]


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Process one DynamoDB Streams batch."""
    records = event.get("Records") or []
    mined = 0
    for record in records:
        if mine_record(record):
            mined += 1
    logger.info("ddb stream batch", extra={"records": len(records), "mined": mined})
    return {"ok": True, "records": len(records), "mined": mined}


def mine_record(record: dict[str, Any]) -> bool:
    """Mine one stream record. Returns ``True`` if examples were written."""
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return False
    new_image = (record.get("dynamodb") or {}).get("NewImage") or {}
    if not is_completed_state_row(new_image):
        return False
    if has_rejections(new_image):
        return False
    run_id = (new_image.get("pk") or {}).get("S", "").removeprefix("RUN#")
    if not run_id:
        return False
    spec_slug = (new_image.get("spec_slug") or {}).get("S")
    project_slug = (new_image.get("project_slug") or {}).get("S", "unknown")
    events = fetch_events(run_id)
    intent = find_intent(events)
    if intent and spec_slug:
        write_intent_to_spec(
            run_id=run_id,
            project_slug=project_slug,
            spec_slug=spec_slug,
            intent=intent,
            events=events,
        )
    write_task_to_diff(run_id=run_id, project_slug=project_slug, events=events)
    return True


def is_completed_state_row(image: dict[str, Any]) -> bool:
    """Was this update a STATE row reaching ``RUN.COMPLETED``?"""
    sk = (image.get("sk") or {}).get("S")
    status = (image.get("status") or {}).get("S")
    return sk == "STATE" and status == "RUN.COMPLETED"


def has_rejections(image: dict[str, Any]) -> bool:
    """Was the run rejected at least once?"""
    raw = (image.get("total_rejections") or {}).get("N", "0")
    if not isinstance(raw, str) or not raw.lstrip("-").isdigit():
        return False
    return int(raw) > 0


def fetch_events(run_id: str) -> list[dict[str, Any]]:
    """Pull the run's event timeline (envelope payloads only)."""
    items: list[dict[str, Any]] = []
    cursor: dict[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": runs_table(),
            "KeyConditionExpression": "pk = :p AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {
                ":p": {"S": f"RUN#{run_id}"},
                ":prefix": {"S": "EVENT#"},
            },
        }
        if cursor is not None:
            kwargs["ExclusiveStartKey"] = cursor
        resp = ddb().query(**kwargs)
        for item in resp.get("Items", []):
            envelope_str = (item.get("envelope") or {}).get("S")
            if envelope_str:
                items.append(json.loads(envelope_str))
        cursor = resp.get("LastEvaluatedKey")
        if cursor is None:
            break
    return items


def find_intent(events: list[dict[str, Any]]) -> str | None:
    """Pull the user's intent string from the REQUEST.RECEIVED envelope."""
    for env in events:
        if env.get("type") == "REQUEST.RECEIVED":
            return (env.get("payload") or {}).get("intent")
    return None


def write_intent_to_spec(
    *,
    run_id: str,
    project_slug: str,
    spec_slug: str,
    intent: str,
    events: list[dict[str, Any]],
) -> None:
    """Persist one ``intent_to_spec`` example."""
    spec_ready = next((e for e in events if e.get("type") == "SPEC.READY"), None)
    record = {
        "schema_version": "1.0",
        "kind": "intent_to_spec",
        "captured_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "project_slug": project_slug,
        "spec_slug": spec_slug,
        "input": {"project_slug": project_slug, "intent": intent},
        "output": (spec_ready or {}).get("payload", {}),
    }
    write_example("intent_to_spec", run_id=run_id, ix=0, record=record)


def write_task_to_diff(
    *,
    run_id: str,
    project_slug: str,
    events: list[dict[str, Any]],
) -> None:
    """Persist one ``task_to_diff`` example per approved task."""
    task_ready_events = [e for e in events if e.get("type") == "TASK.READY"]
    task_approved_events = {
        (e.get("payload") or {}).get("task_id"): e
        for e in events
        if e.get("type") == "TASK.APPROVED"
    }
    for ix, ready in enumerate(task_ready_events):
        payload = ready.get("payload") or {}
        task_id = payload.get("task_id")
        if task_id not in task_approved_events:
            continue
        record = {
            "schema_version": "1.0",
            "kind": "task_to_diff",
            "captured_at": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "project_slug": project_slug,
            "task_id": task_id,
            "input": {
                "project_slug": project_slug,
                "spec_slug": payload.get("spec_slug"),
                "spec_s3_prefix": payload.get("spec_s3_prefix"),
                "task_id": task_id,
            },
            "output": {
                "pr_url": payload.get("pr_url"),
                "diff_summary": payload.get("diff_summary"),
            },
        }
        write_example("task_to_diff", run_id=run_id, ix=ix, record=record)


def write_example(kind: str, *, run_id: str, ix: int, record: dict[str, Any]) -> None:
    """Upload one few-shot example record to S3."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    key = f"evals/few-shots/{kind}/{date}/{run_id}/{ix:04d}.json"
    s3().put_object(
        Bucket=artifacts_bucket(),
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
