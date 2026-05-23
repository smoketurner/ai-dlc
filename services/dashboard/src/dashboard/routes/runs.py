"""POST /v1/runs — emit REQUEST.RECEIVED; projector + Pipe do the rest."""

from __future__ import annotations

import os
import time
from typing import Any

import structlog
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
)
from fastapi import APIRouter, HTTPException, Response, status

from common.runs import start_run
from common.slug import slug_from_repo
from dashboard.auth import CurrentUser
from dashboard.deps import ddb, settings
from dashboard.models import SubmitRunRequest, SubmitRunResponse
from dashboard.repos import TERMINAL_STATUSES

router = APIRouter()
logger = structlog.get_logger()
DDB_BATCH_LIMIT = 25

# Powertools' idempotency utility owns the IN_PROGRESS → COMPLETED state
# machine for /v1/runs. A transient failure during ``start_run`` leaves
# no committed record (the IN_PROGRESS row is cleared on exception), so a
# retry re-executes instead of returning a 409 with a phantom ``run_id``
# that never made it into the runs table.
idempotency_persistence = DynamoDBPersistenceLayer(
    table_name=os.environ["AIDLC_IDEMPOTENCY_TABLE"],
    key_attr="idempotency_key",
    expiry_attr="expires_at",
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath="idempotency_key",
    expires_after_seconds=86_400,
    raise_on_no_idempotency_key=True,
)


@router.post("/v1/runs", response_model=SubmitRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_run(req: SubmitRunRequest, user: CurrentUser) -> SubmitRunResponse:
    """Submit a new run by publishing ``REQUEST.RECEIVED``.

    The projector writes the SUMMARY + EVENT rows on receipt; the
    DDB Stream → Pipe forwards the EVENT row insert to the state-router
    queue as the wake-up beacon.
    """
    project_slug = slug_from_repo(req.target_repo)
    idempotency_key = req.idempotency_key or f"{user.sub}:{int(time.time() * 1000)}"
    try:
        accepted = accept_run(
            trigger={
                "idempotency_key": idempotency_key,
                "project_slug": project_slug,
                "intent": req.intent,
                "requestor": req.requestor or user.sub,
                "requestor_sub": user.sub,
                "target_repo": req.target_repo,
            },
        )
    except IdempotencyAlreadyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "in_progress",
                "message": (
                    "A request with this idempotency_key is already being processed; "
                    "retry with the same key to receive the cached response."
                ),
            },
        ) from exc
    logger.info(
        "run accepted",
        run_id=accepted["run_id"],
        project_slug=project_slug,
        actor=user.sub,
    )
    return SubmitRunResponse(**accepted)


@idempotent_function(
    data_keyword_argument="trigger",
    config=idempotency_config,
    persistence_store=idempotency_persistence,
)
def accept_run(*, trigger: dict[str, Any]) -> dict[str, Any]:
    """Mint a run and publish ``REQUEST.RECEIVED`` under the idempotency layer.

    Keyed on ``trigger["idempotency_key"]``. The record sits at
    ``IN_PROGRESS`` while ``start_run`` runs; on success it's committed
    as ``COMPLETED`` with the returned dict cached for replay; on
    exception it's cleared so a retry re-executes (no phantom
    ``run_id`` left behind).
    """
    run_id, correlation_id = start_run(
        project_slug=trigger["project_slug"],
        intent=trigger["intent"],
        requestor=trigger["requestor"],
        requestor_sub=trigger.get("requestor_sub"),
        target_repo=trigger.get("target_repo"),
    )
    return {
        "run_id": str(run_id),
        "correlation_id": str(correlation_id),
        "project_slug": trigger["project_slug"],
    }


@router.delete("/v1/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: str, user: CurrentUser) -> Response:
    """Hard-delete a terminal run from DynamoDB."""
    cfg = settings()
    summary = fetch_run_summary(run_id, cfg.runs_table)
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    current_status = summary.get("status", {}).get("S", "")
    if current_status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "run_not_terminal", "status": current_status or "unknown"},
        )
    runs_rows = delete_partition(cfg.runs_table, f"RUN#{run_id}")
    logger.info(
        "run deleted",
        run_id=run_id,
        actor=user.sub,
        runs_rows=runs_rows,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def fetch_run_summary(run_id: str, table: str) -> dict[str, Any] | None:
    """Read the SUMMARY row for ``run_id`` from the runs table."""
    resp = ddb().get_item(
        TableName=table,
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "SUMMARY"}},
    )
    return resp.get("Item")


def delete_partition(table: str, pk: str) -> int:
    """Batch-delete every row under ``pk``; returns the count deleted."""
    keys = query_partition_keys(table, pk)
    for chunk_start in range(0, len(keys), DDB_BATCH_LIMIT):
        chunk = keys[chunk_start : chunk_start + DDB_BATCH_LIMIT]
        unprocessed: dict[str, Any] = {
            table: [{"DeleteRequest": {"Key": k}} for k in chunk],
        }
        while unprocessed.get(table):
            resp = ddb().batch_write_item(RequestItems=unprocessed)
            unprocessed = resp.get("UnprocessedItems") or {}
    return len(keys)


def query_partition_keys(table: str, pk: str) -> list[dict[str, Any]]:
    """Page through ``pk`` returning the (pk, sk) keys for every row."""
    keys: list[dict[str, Any]] = []
    start_key: dict[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": table,
            "KeyConditionExpression": "pk = :p",
            "ExpressionAttributeValues": {":p": {"S": pk}},
            "ProjectionExpression": "pk, sk",
        }
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        resp = ddb().query(**kwargs)
        keys.extend({"pk": item["pk"], "sk": item["sk"]} for item in resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if start_key is None:
            return keys
