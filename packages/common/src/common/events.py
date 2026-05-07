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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.ids import CorrelationId, EventId, RunId, new_event_id
from common.runtime import FeedbackItem
from common.validators import NoneSafeList

EventType = Literal[
    "REQUEST.RECEIVED",
    "ISSUE.TRIAGED",
    "SPEC.READY",
    "SPEC.APPROVED",
    "SPEC.REJECTED",
    "CRITIQUE.READY",
    "TASK.READY",
    "TASK.APPROVED",
    "TASK.REJECTED",
    "TASK.ITERATION_REQUESTED",
    "REVIEW.READY",
    "TEST_REPORT.READY",
    "RUN.COMPLETED",
    "RUN.FAILED",
    "RUN.CANCEL_REQUESTED",
    "EVAL.DRIFT_DETECTED",
]

ReviewVerdict = Literal["approve", "request_changes", "comment"]

GateKind = Literal["spec", "task", "deploy", "prod_write"]


class Payload(BaseModel):
    """Base for typed payloads — frozen, strict, no extra keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class UsagePayload(Payload):
    """Payload base that carries per-invocation usage metrics.

    Every agent's ``*.READY`` event mirrors the corresponding ``*Result``
    in :mod:`common.runtime` and carries token / cost / duration so the
    event_projector can accumulate them onto the run's ``STATE`` row.
    """

    token_in: Annotated[int, Field(ge=0)] = 0
    token_out: Annotated[int, Field(ge=0)] = 0
    cost_usd: Annotated[float, Field(ge=0.0)] = 0.0
    duration_ms: Annotated[int, Field(ge=0)] = 0


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
    # Set when the run was kicked off by the Triage agent acting on a
    # GitHub issue. Implementer uses this to write ``Closes <url>`` in the
    # PR body so a merge auto-closes the issue.
    source_issue_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+$"),
        ]
        | None
    ) = None
    # Triage agent's workflow classification. Step Functions branches on
    # this right after the run is recorded — ``spec_driven`` runs the
    # full Architect/Critic/spec-gate flow; ``bug_fix`` / ``upgrade`` /
    # ``docs`` skip the spec phase and run a synthetic 1-task spec the
    # dispatcher generated from the issue context.
    workflow_kind: Literal["spec_driven", "bug_fix", "upgrade", "docs"] = "spec_driven"
    # Slug of the synthetic spec the state_router writes to S3 for
    # non-``spec_driven`` runs (bug_fix / upgrade / docs). ``None`` for
    # ``spec_driven`` — the Architect produces the spec at runtime.
    synthetic_spec_slug: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class IssueTriaged(Payload):
    """The Triage agent has classified a tagged GitHub issue.

    Step Functions branches on :attr:`action` (and :attr:`workflow_kind`
    when ``action == "proceed"``). The full :class:`common.triage.TriageDecision`
    object is stored at :attr:`decision_s3_key`; the dashboard renders
    the decision history per repo.
    """

    project_slug: str
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    issue_url: Annotated[
        str, Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+$")
    ]
    issue_number: Annotated[int, Field(ge=1)]
    action: Literal["proceed", "ask", "defer", "decline"]
    workflow_kind: Literal["spec_driven", "bug_fix", "upgrade", "docs"] | None = None
    decision_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    rationale: Annotated[str, Field(min_length=1, max_length=2048)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    session_id: str


class SpecReady(UsagePayload):
    """The architect agent has produced a spec bundle and is awaiting approval.

    The bundle is the three-document set (requirements, design, tasks) under
    ``s3://artifacts/specs/{spec_slug}/`` plus any new ADRs proposed in the
    design that are committed under ``docs/ADRs/`` when the spec is approved.

    ``task_ids`` carries the per-task identifiers the Architect minted; the
    event_projector persists them on the run STATE row so the state-router's
    ``spec_approved`` handler can seed one TASK row per id without re-reading
    the spec from S3.
    """

    project_slug: str
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    requirements_summary: Annotated[str, Field(max_length=1024)]
    design_summary: Annotated[str, Field(max_length=1024)]
    task_count: Annotated[int, Field(ge=1)]
    task_ids: Annotated[list[str], Field(min_length=1, max_length=64)]
    proposed_adrs: NoneSafeList[str] = Field(default_factory=list)
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


class CritiqueReady(UsagePayload):
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


class TaskReady(UsagePayload):
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


class TaskIterationRequested(Payload):
    """A webhook reported a PR signal that should trigger the implementer.

    Emitted by the dashboard webhook handler on CI failure, review
    ``changes_requested``, or a bot @-mention in a review or PR comment.
    Carries enough context (one :class:`FeedbackItem`) for the state
    router to dispatch the implementer with concrete feedback when it
    transitions the task to ``iterating``.

    The event_projector appends ``delivery_id`` to the task row's
    ``delivery_ids`` set for idempotency — duplicate webhook deliveries
    do not stack triggers.
    """

    project_slug: str
    spec_slug: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    delivery_id: Annotated[str, Field(min_length=1, max_length=128)]
    feedback: FeedbackItem


class RunCancelRequested(Payload):
    """A user or system requested cancellation of an in-flight run.

    Emitted by the dashboard webhook on ``issues.unassigned`` (the bot
    was unassigned from the source issue) or on an ``/aidlc cancel``
    PR/issue comment, and may be emitted by other surfaces (dashboard
    button) as the cancellation UX expands.
    """

    project_slug: str
    requestor: Annotated[str, Field(min_length=1, max_length=128)]
    source: Literal[
        "issue_unassigned",
        "comment_command",
        "dashboard",
    ]
    reason: Annotated[str, Field(max_length=512)] | None = None


class ReviewReady(UsagePayload):
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


class TestReportReady(UsagePayload):
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
    """The run reached its terminal success state.

    Run-level usage totals (token_in/out, cost_usd, duration_ms) are
    accumulated on the run's STATE row by :mod:`event_projector` from
    each ``*.READY`` event's :class:`UsagePayload`; readers of cumulative
    metrics query the STATE row directly, not this event.
    """

    project_slug: str
    spec_slug: str
    tasks_completed: int


class RunFailed(Payload):
    """The run reached a terminal failure state."""

    project_slug: str
    failed_state: str
    error_class: str
    error_message: str
    retryable: bool


type AnyPayload = (
    RequestReceived
    | IssueTriaged
    | SpecReady
    | SpecApproved
    | SpecRejected
    | CritiqueReady
    | TaskReady
    | TaskApproved
    | TaskRejected
    | TaskIterationRequested
    | ReviewReady
    | TestReportReady
    | RunCompleted
    | RunFailed
    | RunCancelRequested
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


class IncomingEnvelope[PayloadT: Payload](EventEnvelope[PayloadT]):
    """Wire-format variant for parsing incoming EventBridge events.

    Identical fields to :class:`EventEnvelope`, but with ``strict=False`` so
    Pydantic coerces ISO-8601 datetime strings (the on-the-wire timestamp
    representation) into :class:`datetime` instances. The strict envelope is
    used at *emission* — we control the shape and want any drift to fail
    loud. This permissive sibling is used at *ingestion* by Lambdas that
    parse events back off the bus.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class UntypedEnvelope(BaseModel):
    """Wire-format variant carrying the envelope fields with an untyped payload.

    Use when a Lambda needs the validated envelope metadata (``type``,
    ``run_id``, ``timestamp``, etc.) but operates on payloads structurally
    rather than against a known typed-payload union. Avoids the strict
    union-discrimination Pydantic does in :class:`IncomingEnvelope` when
    every payload field would have to satisfy one of the typed payload
    classes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)

    schema_version: Literal["1.0"] = "1.0"
    event_id: EventId
    type: EventType
    timestamp: datetime
    run_id: RunId
    correlation_id: CorrelationId
    causation_id: EventId | None = None
    actor_id: Annotated[str, Field(min_length=1, max_length=128)]
    payload: dict[str, Any]
