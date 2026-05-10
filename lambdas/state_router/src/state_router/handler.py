"""SQS-driven state router for the SDLC pipeline.

The beacon is an interrupt, not a heartbeat: a beacon means "the router
has work to do right now". Each receive triggers this handler:

1. Read the run's STATE row + every TASK row from DynamoDB.
2. Compute the next :data:`~.actions.Action` via :func:`.dispatch.decide`.
3. Execute the action via :func:`.execute.execute` — invoke an agent,
   emit an event, write a synthetic spec, etc.
4. Delete the beacon (success ack to SQS). The next state-advancing
   event re-emits a fresh beacon from the projector.

Implication: while a run is parked on a Noop (waiting on a human PR
review, an agent's async response, etc.), the queue is empty and the
router does not run. The webhook → EventBridge → projector pipeline is
the wake-up edge.

The router never advances state on its own initiative for "what
happened in the world" transitions — those go through the projector
applying events. The router does, however, write the per-action
``*_running`` cursor (or other internal bookkeeping advances) using
DDB ``ConditionExpression`` so concurrent routers can't dispatch the
same agent twice.
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from state_router.aws import ddb
from state_router.config import runs_table
from state_router.dispatch import decide
from state_router.execute import execute
from state_router.model import Run, parse_run

logger = Logger(service="state_router")
tracer = Tracer(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Process every SQS record in the batch.

    Each beacon is processed once per Lambda invocation and then deleted
    by SQS (the handler returns no batch-item failures). If
    :func:`process_record` raises, the exception propagates and SQS
    redelivers under its standard error semantics; that is the only
    path that keeps a beacon visible.

    The empty ``batchItemFailures`` list is preserved in the response
    shape so the event source mapping's
    ``function_response_types=["ReportBatchItemFailures"]`` setting
    keeps working — switching it off would require an infrastructure
    change.
    """
    records = event.get("Records") or []
    for record in records:
        process_record(record)
    metrics.add_metric(name="BeaconsProcessed", unit=MetricUnit.Count, value=len(records))
    return {"batchItemFailures": []}


def process_record(record: dict[str, Any]) -> None:
    """Decode + dispatch one beacon.

    Always succeeds (returns ``None``) once the message body has been
    parsed: malformed / orphan / terminal beacons are logged and the
    SQS ack lets the message be deleted. State-advancing work is the
    projector's responsibility; the next event from the platform bus
    re-emits a beacon that the router will pick up.
    """
    try:
        body = json.loads(record.get("body") or "{}")
    except json.JSONDecodeError:
        logger.warning("malformed beacon body", extra={"messageId": record.get("messageId")})
        return
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        logger.warning("beacon missing run_id", extra={"body": body})
        return
    run = read_run(run_id)
    if run is None:
        logger.info("orphan beacon — no run row", extra={"run_id": run_id})
        return
    action = decide(run)
    execute(run, action)


@tracer.capture_method
def read_run(run_id: str) -> Run | None:
    """Fetch the run's STATE row + every TASK row in one Query."""
    resp = ddb().query(
        TableName=runs_table(),
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": f"RUN#{run_id}"}},
    )
    items = resp.get("Items") or []
    state_item: dict[str, Any] = {}
    task_items: list[dict[str, Any]] = []
    for item in items:
        sk = item.get("sk", {}).get("S", "")
        if sk == "STATE":
            state_item = item
        elif sk.startswith("TASK#"):
            task_items.append(item)
    return parse_run(state_item, task_items)
