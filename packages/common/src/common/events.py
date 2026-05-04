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
    → SPEC.READY        (Architect writes requirements + design + tasks)
    → CRITIQUE.READY    (Critic adversarially reviews the spec — advisory)
    → SPEC.APPROVED     (gate 1 — human reviewer)
    → TASK.READY        ┐
    → REVIEW.READY      │ Reviewer code-reviews the PR — advisory
    → TEST_REPORT.READY │ Tester flags test gaps — advisory
    → TASK.APPROVED     │ loop while tasks remain
    → ...               ┘
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
    "CRITIQUE.READY",
    "TASK.READY",
    "TASK.APPROVED",
    "TASK.REJECTED",
    "REVIEW.READY",
    "TEST_REPORT.READY",
    "RUN.COMPLETED",
    "RUN.FAILED",
]

ReviewVerdict = Literal["approve", "request_changes", "comment"]

GateKind = Literal["spec", "task", "deploy", "prod_write"]


class Payload(BaseModel):
    """Base for typed payloads — frozen, strict, no extra keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def now() -> datetime:
    """Return the current time in UTC, tz-aware."""
    return datetime.now(UTC)


class RequestReceived(Payload):
    """A new run request has entered the system.

    ``requestor`` is the human-readable identity submitted via the form
    (typically an email). ``requestor_sub`` is the stable Cognito subject
    identifier — used as the user_id when fetching this user's GitHub
    OAuth token from the AgentCore Identity Token Vault. ``target_repo``
    is the GitHub repository (``owner/name``) the agents should operate
    on for this run.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    requestor: Annotated[str, Field(min_length=1, max_length=128)]
    requestor_sub: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    target_repo: (
        Annotated[
            str,
            Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$"),
        ]
        | None
    ) = None


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


class CritiqueReady(Payload):
    """Critic agent produced an adversarial review of the spec — advisory.

    The critique is rendered to ``s3://artifacts/runs/{run_id}/critique.md``;
    the event payload references it via :attr:`critique_s3_key`. This event
    does not gate the pipeline — humans still own SPEC.APPROVED.
    """

    project_slug: str
    spec_slug: str
    critique_s3_key: str
    issue_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


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


class ReviewReady(Payload):
    """Reviewer agent code-reviewed a task PR — advisory.

    Comments are posted to the PR via ``repo_helper.comment_pr``; this event
    surfaces the verdict and counts to the dashboard. Does not gate the
    pipeline — humans still own TASK.APPROVED.
    """

    project_slug: str
    spec_slug: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    verdict: ReviewVerdict
    comment_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class TestReportReady(Payload):
    """Tester agent identified test gaps in a task PR — advisory.

    Test gap suggestions are posted to the PR via ``repo_helper.comment_pr``;
    this event records counts and a summary for the dashboard. Does not gate
    the pipeline.
    """

    __test__ = False  # opt out of pytest collection (Test*-prefix collision)

    project_slug: str
    spec_slug: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    gap_count: Annotated[int, Field(ge=0)]
    suggested_test_count: Annotated[int, Field(ge=0)]
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


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
    | CritiqueReady
    | TaskReady
    | TaskApproved
    | TaskRejected
    | ReviewReady
    | TestReportReady
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
