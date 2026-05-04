"""POST /v1/runs — submits a run by publishing REQUEST.RECEIVED to the bus."""

from __future__ import annotations

import json
import time

import structlog
from fastapi import APIRouter, HTTPException, status

from common.events import EventEnvelope, RequestReceived
from common.ids import new_correlation_id, new_event_id, new_run_id
from dashboard.auth import CurrentUser
from dashboard.deps import ddb, events, settings
from dashboard.models import SubmitRunRequest, SubmitRunResponse

router = APIRouter()
logger = structlog.get_logger()


@router.post("/v1/runs", response_model=SubmitRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_run(req: SubmitRunRequest, user: CurrentUser) -> SubmitRunResponse:
    """Submit a new run and emit ``REQUEST.RECEIVED``."""
    cfg = settings()
    project_slug = slug_from_repo(req.target_repo)
    idempotency_key = req.idempotency_key or f"{user.sub}:{int(time.time() * 1000)}"
    run_id = new_run_id()
    if not reserve_idempotency(idempotency_key, str(run_id), cfg.idempotency_table):
        existing = fetch_existing_run(idempotency_key, cfg.idempotency_table)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "idempotent_replay", "run_id": existing or "unknown"},
        )
    correlation_id = new_correlation_id()
    envelope = EventEnvelope[RequestReceived](
        event_id=new_event_id(),
        type="REQUEST.RECEIVED",
        run_id=run_id,
        correlation_id=correlation_id,
        actor_id=req.requestor or user.sub,
        payload=RequestReceived(
            project_slug=project_slug,
            intent=req.intent,
            requestor=req.requestor or user.sub,
            requestor_sub=user.sub,
            target_repo=req.target_repo,
        ),
    )
    publish(envelope, cfg.bus_name)
    logger.info(
        "run accepted",
        run_id=str(run_id),
        project_slug=project_slug,
        actor=user.sub,
    )
    return SubmitRunResponse(
        run_id=str(run_id),
        correlation_id=str(correlation_id),
        project_slug=project_slug,
    )


def slug_from_repo(target_repo: str) -> str:
    """``owner/name`` -> ``owner-name`` (lowercased). One slug per repo, stable across runs."""
    return target_repo.lower().replace("/", "-")


def reserve_idempotency(key: str, run_id: str, table: str) -> bool:
    """Conditional put on the idempotency table; ``True`` on first reservation."""
    expires_at = int(time.time()) + 86400
    try:
        ddb().put_item(
            TableName=table,
            Item={
                "idempotency_key": {"S": key},
                "run_id": {"S": run_id},
                "expires_at": {"N": str(expires_at)},
            },
            ConditionExpression="attribute_not_exists(idempotency_key)",
        )
    except ddb().exceptions.ConditionalCheckFailedException:
        return False
    return True


def fetch_existing_run(key: str, table: str) -> str | None:
    """Return the previously reserved run_id for ``key``, if any."""
    resp = ddb().get_item(
        TableName=table,
        Key={"idempotency_key": {"S": key}},
        ProjectionExpression="run_id",
    )
    item = resp.get("Item")
    if item is None:
        return None
    return item["run_id"]["S"]


def publish(envelope: EventEnvelope[RequestReceived], bus_name: str) -> None:
    """Emit a REQUEST.RECEIVED event to the platform bus."""
    events().put_events(
        Entries=[
            {
                "Source": f"ai-dlc.{envelope.actor_id}",
                "DetailType": envelope.type,
                "Detail": envelope.model_dump_json(),
                "EventBusName": bus_name,
            },
        ],
    )
    json.dumps(envelope.model_dump_json())  # ensure serialisability for ty
