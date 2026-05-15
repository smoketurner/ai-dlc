"""SQS-driven state router — events-only edition.

The beacon is an interrupt, not a heartbeat. Each beacon means "an
event landed on the platform bus for this run — re-read the event log
and decide what to do next." The handler:

1. Decodes the beacon body → ``run_id``.
2. Queries DynamoDB for every ``EVENT#*`` row on ``pk=RUN#{run_id}``.
3. Parses the JSON envelopes (oldest first — DDB ``sk`` is a UUID7).
4. Calls :func:`state_router.decide.decide` to get the next action.
5. Calls :func:`state_router.execute.execute` to apply it.

Implications:

* Replaying any prefix of events is safe — :func:`decide` is pure.
* The router never writes to DDB. Every state-affecting change goes
  through the platform bus → projector.
* Lambda crashes are SQS-retried. Same beacon redelivered = same
  events read = same action computed = same side effect (modulo the
  AgentCore session-id idempotency on agent invokes).
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from common.events import UntypedEnvelope
from state_router.aws import ddb
from state_router.config import runs_table
from state_router.decide import decide
from state_router.execute import execute

logger = Logger(service="state_router")
tracer = Tracer(service="state_router")
metrics = Metrics(namespace="ai-dlc", service="state_router")


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Process every SQS record in the batch.

    Returns ``{"batchItemFailures": []}`` so the event source mapping's
    ``function_response_types=["ReportBatchItemFailures"]`` setting stays
    happy. Uncaught exceptions still propagate and trigger SQS retry.
    """
    records = event.get("Records") or []
    for record in records:
        process_record(record)
    metrics.add_metric(name="BeaconsProcessed", unit=MetricUnit.Count, value=len(records))
    return {"batchItemFailures": []}


def process_record(record: dict[str, Any]) -> None:
    """Decode + dispatch one beacon.

    Malformed bodies, missing run ids, and runs with no events all
    succeed (ack the message) — the platform's wake-up edge is the
    projector emitting new events, not redelivery of stale beacons.
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
    events = read_events(run_id)
    if not events:
        logger.info("no events for run", extra={"run_id": run_id})
        return
    action = decide(events)
    execute(action, events)


@tracer.capture_method
def read_events(run_id: str) -> list[UntypedEnvelope]:
    """Fetch every ``EVENT#*`` row for ``run_id`` and parse the envelopes.

    Returns events in time order (DDB sort key is a UUID7, so the
    natural query order is chronological).
    """
    items = query_all(f"RUN#{run_id}")
    envelopes: list[UntypedEnvelope] = []
    for item in items:
        sk = (item.get("sk") or {}).get("S", "")
        if not sk.startswith("EVENT#"):
            continue
        envelope_json = (item.get("envelope") or {}).get("S")
        if not envelope_json:
            continue
        try:
            envelope = UntypedEnvelope.model_validate_json(envelope_json)
        except ValidationError as exc:
            logger.warning(
                "skipping unparseable event",
                extra={"run_id": run_id, "sk": sk, "errors": exc.errors()},
            )
            continue
        envelopes.append(envelope)
    envelopes.sort(key=lambda env: str(env.event_id))
    return envelopes


def query_all(pk: str) -> list[dict[str, Any]]:
    """Run a paginated Query across all sk values for ``pk``."""
    items: list[dict[str, Any]] = []
    last_evaluated: dict[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": runs_table(),
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
        }
        if last_evaluated is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated
        response = ddb().query(**kwargs)
        items.extend(response.get("Items") or [])
        last_evaluated = response.get("LastEvaluatedKey")
        if not last_evaluated:
            break
    return items
