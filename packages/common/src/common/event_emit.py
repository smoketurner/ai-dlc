"""EventBridge publish helper for agents emitting their completion events.

Each agent (Architect, Critic, Implementer, Reviewer, Tester, Triage)
publishes its own ``*.READY`` / ``*.TRIAGED`` event when finished. The
event_projector picks them up off the platform bus and applies state
transitions; the dashboard timeline reads the same stream.

This helper centralises the EventBridge ``PutEvents`` call so the bus
name resolution, source-field derivation, and JSON serialisation are
identical across agents. It uses ``AIDLC_BUS_NAME`` from the environment
(set by the Terraform agentcore_runtime module on every agent runtime).
"""

from __future__ import annotations

import logging
import os
from functools import cache
from typing import TYPE_CHECKING

import boto3

from common.errors import EventEmitError
from common.events import EventEnvelope, Payload

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient

logger = logging.getLogger(__name__)


@cache
def events_client() -> EventBridgeClient:
    """Process-cached EventBridge client."""
    return boto3.client("events")


def bus_name() -> str:
    """Platform EventBridge bus name (set by Terraform on every runtime)."""
    return os.environ["AIDLC_BUS_NAME"]


def publish[PayloadT: Payload](envelope: EventEnvelope[PayloadT]) -> None:
    """Emit ``envelope`` onto the platform EventBridge bus.

    The bus's ``Source`` is derived from the envelope's ``actor_id`` (e.g.
    ``ai-dlc.reviewer``) and ``DetailType`` is the envelope's ``type``.
    Same shape entry_adapter and the SFN PutEvents states produce, so
    downstream consumers (event_projector, EventBridge rules) don't need
    to special-case agent-emitted events.

    ``PutEvents`` can return HTTP 200 while still rejecting individual
    entries (``FailedEntryCount > 0``), so the response is inspected and
    :class:`~common.errors.EventEmitError` is raised when any entry failed.
    Raising lets SQS/Lambda retry the invocation and keeps drops out of
    the silent-success path that would wedge runs in non-terminal states.
    """
    response = events_client().put_events(
        Entries=[
            {
                "Source": f"ai-dlc.{envelope.actor_id}",
                "DetailType": envelope.type,
                "Detail": envelope.model_dump_json(),
                "EventBusName": bus_name(),
            },
        ],
    )
    failed_count = response.get("FailedEntryCount", 0)
    if failed_count:
        entry = response.get("Entries", [{}])[0]
        raise EventEmitError(
            "EventBridge rejected event entry",
            type=envelope.type,
            run_id=str(envelope.run_id),
            event_id=str(envelope.event_id),
            failed_entry_count=failed_count,
            error_code=entry.get("ErrorCode"),
            error_message=entry.get("ErrorMessage"),
        )
    logger.info(
        "event published",
        extra={
            "type": envelope.type,
            "run_id": str(envelope.run_id),
            "event_id": str(envelope.event_id),
        },
    )


def try_publish[PayloadT: Payload](envelope: EventEnvelope[PayloadT]) -> None:
    """Publish ``envelope``, logging :class:`EventEmitError` instead of raising.

    Use this from agent exception handlers (the daemon-thread body has no
    SQS/Lambda redrive — a raised ``EventEmitError`` would propagate out of the
    thread and wedge the run with only a ``*.DISPATCHED`` marker). Success-path
    emissions should keep using :func:`publish`, which raises so the
    state-router's SQS retry path stays load-bearing.
    """
    try:
        publish(envelope)
    except EventEmitError:
        logger.exception(
            "try_publish: event emission failed",
            extra={
                "type": envelope.type,
                "run_id": str(envelope.run_id),
                "event_id": str(envelope.event_id),
            },
        )
