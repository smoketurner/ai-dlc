"""Event envelope and typed payloads for the ai-dlc EventBridge bus.

Every event the platform emits — whether from a Lambda, a Step Functions task,
or an agent — uses :class:`EventEnvelope` as its outer shape so we can version
the schema, thread correlation IDs, and route on ``type`` without parsing the
payload first.

The envelope and payload are kept structurally separate from the EventBridge
``PutEvents`` envelope (``source``, ``detail-type``, ``detail``) so we could
move off EventBridge without rewriting any model. The bus fields are derived
from envelope fields at publish time.

The platform follows a spec-driven SDLC pipeline:

  REQUEST.RECEIVED
    → SPEC.READY     (Architect writes requirements + design + tasks)
    → SPEC.APPROVED  (gate 1)
    → TASK.READY     ┐
    → TASK.APPROVED  │ loop while tasks remain
    → ...            ┘
    → RUN.COMPLETED
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.ids import CorrelationId, EventId, RunId, new_event_id

EventType = Literal[
    "REQUEST.RECEIVED",
    "SPEC.READY",
    "SPEC.APPROVED",
    "SPEC.REJECTED",
    "TASK.READY",
    "TASK.APPROVED",
    "TASK.REJECTED",
    "RUN.COMPLETED",
    "RUN.FAILED",
]

GateKind = Literal["spec", "task", "deploy", "prod_write"]


class Payload(BaseModel):
    """Base for typed payloads — frozen, strict, no extra keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def now() -> datetime:
    """Return the current time in UTC, tz-aware."""
    return datetime.now(UTC)


class RequestReceived(Payload):
    """A new run request has entered the system."""

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    requestor: Annotated[str, Field(min_length=1, max_length=128)]


class SpecReady(Payload):
    """The architect agent has produced a spec bundle and is awaiting approval.

    The bundle is the three-document set (requirements, design, tasks) under
    ``s3://artifacts/specs/{spec_slug}/`` plus any new ADRs proposed in the
    design that are committed under ``docs/ADRs/`` when the spec is approved.
    """

    project_slug: str
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    requirements_summary: Annotated[str, Field(max_length=1024)]
    design_summary: Annotated[str, Field(max_length=1024)]
    task_count: Annotated[int, Field(ge=1)]
    proposed_adrs: list[str] = Field(default_factory=list)
    session_id: str


class SpecApproved(Payload):
    """A reviewer approved the spec; task execution may begin."""

    project_slug: str
    spec_slug: str
    spec_s3_prefix: str
    reviewer: str
    comment: str | None = None


class SpecRejected(Payload):
    """A reviewer rejected the spec. The architect may retry with feedback."""

    project_slug: str
    spec_slug: str
    spec_s3_prefix: str
    reviewer: str
    reason: str


class TaskReady(Payload):
    """The implementer agent opened a PR for one task and is awaiting approval."""

    project_slug: str
    spec_slug: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str


class TaskApproved(Payload):
    """A reviewer approved the task PR. The next task may start."""

    project_slug: str
    spec_slug: str
    task_id: str
    pr_url: str
    reviewer: str


class TaskRejected(Payload):
    """A reviewer rejected the task PR. The implementer may retry with feedback."""

    project_slug: str
    spec_slug: str
    task_id: str
    pr_url: str
    reviewer: str
    reason: str


class RunCompleted(Payload):
    """The run reached its terminal success state."""

    project_slug: str
    spec_slug: str
    tasks_completed: int
    total_duration_ms: int
    total_token_in: int
    total_token_out: int
    total_cost_usd: float


class RunFailed(Payload):
    """The run reached a terminal failure state."""

    project_slug: str
    failed_state: str
    error_class: str
    error_message: str
    retryable: bool


type AnyPayload = (
    RequestReceived
    | SpecReady
    | SpecApproved
    | SpecRejected
    | TaskReady
    | TaskApproved
    | TaskRejected
    | RunCompleted
    | RunFailed
)


class EventEnvelope[PayloadT: Payload](BaseModel):
    """Versioned envelope wrapping a typed payload.

    The envelope sits inside the EventBridge ``detail`` field. The bus's own
    ``source`` and ``detail-type`` are derived from :attr:`actor_id` and
    :attr:`type` at publish time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1.0"] = "1.0"
    event_id: EventId = Field(default_factory=new_event_id)
    type: EventType
    timestamp: datetime = Field(default_factory=now)
    run_id: RunId
    correlation_id: CorrelationId
    causation_id: EventId | None = None
    actor_id: Annotated[str, Field(min_length=1, max_length=128)]
    payload: PayloadT
