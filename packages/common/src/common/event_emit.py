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

import os
from functools import cache
from typing import TYPE_CHECKING

import boto3
import structlog

from common.events import EventEnvelope, Payload

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient

logger = structlog.get_logger()


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
    """
    events_client().put_events(
        Entries=[
            {
                "Source": f"ai-dlc.{envelope.actor_id}",
                "DetailType": envelope.type,
                "Detail": envelope.model_dump_json(),
                "EventBusName": bus_name(),
            },
        ],
    )
    logger.info(
        "event published",
        type=envelope.type,
        run_id=str(envelope.run_id),
        event_id=str(envelope.event_id),
    )
