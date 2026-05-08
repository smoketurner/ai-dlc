"""SQS-driven state router for the SDLC pipeline.

One beacon message per active run sits on the ``state-router`` queue.
Each receive triggers this handler:

1. Read the run's STATE row + every TASK row from DynamoDB.
2. Compute the next :data:`~.actions.Action` via :func:`.dispatch.decide`.
3. Execute the action via :func:`.execute.execute` — invoke an agent,
   emit an event, write a synthetic spec, etc.
4. Leave the beacon undeleted so the visibility timeout expires and the
   queue re-delivers the message; the next poll will see whatever the
   projector has advanced to in the meantime. Terminal runs delete the
   beacon directly.

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

from common.state import TERMINAL_RUN_STATES
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

    Each beacon is processed once per Lambda invocation, then either:

    * **Reported as a batch-item failure** (the default for non-terminal
      runs) — Lambda's SQS event source mapping leaves the message
      visible after the queue's visibility timeout, redelivering it on
      the next poll. This is how the state machine ticks forward
      between agent completion / webhook events.
    * **Returned as a successful record** (terminal, orphan, or malformed
      beacons) — Lambda auto-deletes those messages on success, so the
      beacon is gone for good.

    Requires ``function_response_types=["ReportBatchItemFailures"]`` on
    the event source mapping. The beacon queue has no DLQ + no
    ``maxReceiveCount`` cap — a beacon cycles indefinitely until the
    run reaches a terminal state. SQS-level pathology surfaces via
    CloudWatch alarms on receive-count age, not via DLQ-by-redelivery.
    """
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []
    for record in records:
        if process_record(record):
            failures.append({"itemIdentifier": record["messageId"]})
    metrics.add_metric(name="BeaconsProcessed", unit=MetricUnit.Count, value=len(records))
    return {"batchItemFailures": failures}


def process_record(record: dict[str, Any]) -> bool:
    """Decode + dispatch one beacon.

    Returns ``True`` when the beacon should remain in the queue (active
    run, must keep cycling) and ``False`` when SQS should delete it
    (terminal / orphan / malformed). The handler reports the ``True``
    cases as ``batchItemFailures`` to keep them visible.
    """
    try:
        body = json.loads(record.get("body") or "{}")
    except json.JSONDecodeError:
        logger.warning("malformed beacon body", extra={"messageId": record.get("messageId")})
        return False
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        logger.warning("beacon missing run_id", extra={"body": body})
        return False
    run = read_run(run_id)
    if run is None:
        logger.info("orphan beacon — no run row", extra={"run_id": run_id})
        return False
    if run.current_state in TERMINAL_RUN_STATES:
        logger.info(
            "terminal run, releasing beacon",
            extra={"run_id": run_id, "state": str(run.current_state)},
        )
        return False
    action = decide(run)
    execute(run, action)
    return True


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
