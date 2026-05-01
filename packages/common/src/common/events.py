"""Event envelope and typed payloads for the ai-dlc EventBridge bus.

Every event the platform emits — whether from a Lambda, a Step Functions task,
or an agent — uses :class:`EventEnvelope` as its outer shape so we can version
the schema, thread correlation IDs, and route on ``type`` without parsing the
payload first.

The envelope and payload are kept structurally separate from the EventBridge
``PutEvents`` envelope (``source``, ``detail-type``, ``detail``) so we could
move off EventBridge without rewriting any model. The bus fields are derived
from envelope fields at publish time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.ids import CorrelationId, EventId, RunId, new_event_id

EventType = Literal[
    "REQUEST.RECEIVED",
    "ARCH.READY",
    "ARCH.APPROVED",
    "ARCH.REJECTED",
    "IMPL.READY",
    "IMPL.APPROVED",
    "IMPL.REJECTED",
    "RUN.COMPLETED",
    "RUN.FAILED",
]

GateKind = Literal["adr", "pr", "deploy", "prod_write"]


class _Payload(BaseModel):
    """Base for typed payloads — frozen, strict, no extra keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _now() -> datetime:
    """Return the current time in UTC, tz-aware."""
    return datetime.now(UTC)


class RequestReceived(_Payload):
    """A new run request has entered the system."""

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    requestor: Annotated[str, Field(min_length=1, max_length=128)]


class ArchReady(_Payload):
    """The architect agent has produced an ADR and is awaiting approval."""

    project_slug: str
    adr_s3_key: str
    summary: Annotated[str, Field(max_length=1024)]
    session_id: str


class ArchApproved(_Payload):
    """A reviewer approved the ADR; implementation may begin."""

    project_slug: str
    adr_s3_key: str
    reviewer: str
    comment: str | None = None


class ArchRejected(_Payload):
    """A reviewer rejected the ADR. The architect may retry with feedback."""

    project_slug: str
    adr_s3_key: str
    reviewer: str
    reason: str


class ImplReady(_Payload):
    """The implementer agent opened a code PR and is awaiting approval."""

    project_slug: str
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str


class ImplApproved(_Payload):
    """A reviewer approved the implementation PR."""

    project_slug: str
    pr_url: str
    reviewer: str


class ImplRejected(_Payload):
    """A reviewer rejected the implementation PR."""

    project_slug: str
    pr_url: str
    reviewer: str
    reason: str


class RunCompleted(_Payload):
    """The run reached its terminal success state."""

    project_slug: str
    total_duration_ms: int
    total_token_in: int
    total_token_out: int
    total_cost_usd: float


class RunFailed(_Payload):
    """The run reached a terminal failure state."""

    project_slug: str
    failed_state: str
    error_class: str
    error_message: str
    retryable: bool


type AnyPayload = (
    RequestReceived
    | ArchReady
    | ArchApproved
    | ArchRejected
    | ImplReady
    | ImplApproved
    | ImplRejected
    | RunCompleted
    | RunFailed
)


class EventEnvelope[PayloadT: _Payload](BaseModel):
    """Versioned envelope wrapping a typed payload.

    The envelope sits inside the EventBridge ``detail`` field. The bus's own
    ``source`` and ``detail-type`` are derived from :attr:`actor_id` and
    :attr:`type` at publish time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1.0"] = "1.0"
    event_id: EventId = Field(default_factory=new_event_id)
    type: EventType
    timestamp: datetime = Field(default_factory=_now)
    run_id: RunId
    correlation_id: CorrelationId
    causation_id: EventId | None = None
    actor_id: Annotated[str, Field(min_length=1, max_length=128)]
    payload: PayloadT
