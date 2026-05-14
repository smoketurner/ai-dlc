"""Event envelope and typed payloads for the ai-dlc EventBridge bus.

Every event the platform emits — whether from a Lambda, a Step Functions task,
or an agent — uses :class:`EventEnvelope` as its outer shape so we can version
the schema, thread correlation IDs, and route on ``type`` without parsing the
payload first.

The envelope and payload are kept structurally separate from the EventBridge
``PutEvents`` envelope (``source``, ``detail-type``, ``detail``) so we could
move off EventBridge without rewriting any model. The bus fields are derived
from envelope fields at publish time.

The platform follows a single-PR-per-issue SDLC pipeline:

  REQUEST.RECEIVED
    → ISSUE.TRIAGED       (Triage classifies a tagged GitHub issue)
    → DESIGN.READY        (Architect writes plan.md to S3)
    → CRITIQUE.READY      (Critic adversarially reviews the plan — advisory)
    → IMPL_PR.OPENED      (Implementer opens the single impl PR)
    → REVIEW.READY        (Reviewer code-reviews the PR — gating)
    → TEST_REPORT.READY   (Tester flags test gaps — advisory)
    → CODE_CRITIQUE.READY (Code-Critic adversarial review — advisory)
    → CHECKS.PASSED       (all required GitHub Checks green) →
                          awaiting_human_merge → RUN.COMPLETED
    or
    → CHECKS.FAILED       → revising → REVISION.READY → re-validate
    or
    → IMPL.ITERATION_REQUESTED (human @aidlc-bot mention) → revising → ...
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.ids import CorrelationId, EventId, RunId, new_event_id

EventType = Literal[
    "REQUEST.RECEIVED",
    "ISSUE.TRIAGED",
    "DESIGN.READY",
    "CRITIQUE.READY",
    "IMPL_PR.OPENED",
    "IMPL.ITERATION_REQUESTED",
    "CHECKS.PASSED",
    "CHECKS.FAILED",
    "REVIEW.READY",
    "TEST_REPORT.READY",
    "CODE_CRITIQUE.READY",
    "REVISION.READY",
    "RUN.COMPLETED",
    "RUN.FAILED",
    "RUN.CANCEL_REQUESTED",
    "EVAL.DRIFT_DETECTED",
]

ReviewVerdict = Literal["approve", "request_changes", "comment"]

CheckConclusion = Literal[
    "success", "failure", "timed_out", "cancelled", "action_required", "stale"
]


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


class IssueTriaged(Payload):
    """The Triage agent has classified a tagged GitHub issue.

    The full :class:`common.triage.TriageDecision` object is stored at
    :attr:`decision_s3_key`; the dashboard renders the decision history
    per repo. ``action="proceed"`` advances the run to architect dispatch;
    ``action="research"`` branches to the Proposer; ``ask`` / ``defer`` /
    ``decline`` terminate the run (with an explanatory comment on the issue).
    """

    project_slug: str
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    issue_url: Annotated[
        str, Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+$")
    ]
    issue_number: Annotated[int, Field(ge=1)]
    action: Literal["proceed", "ask", "defer", "decline", "research"]
    decision_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    rationale: Annotated[str, Field(min_length=1, max_length=2048)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    session_id: str


class DesignReady(UsagePayload):
    """The Architect has produced an implementation plan.

    The plan is a single markdown document at ``s3://artifacts/runs/{run_id}/plan.md``
    structured like a Claude Code plan-mode plan: Context, Assumptions,
    Approach, Files-to-modify, Reuse, Implementation-steps, Verification,
    Out-of-scope. The Critic reads this next; advances run to ``designed``.
    """

    project_slug: str
    plan_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class CritiqueReady(UsagePayload):
    """Critic agent produced an adversarial review of the architect's plan — advisory.

    The critique is rendered to ``s3://artifacts/runs/{run_id}/critique.md``;
    the event payload references it via :attr:`critique_s3_key`. This event
    does not gate the pipeline — its findings inform the implementer.
    """

    project_slug: str
    critique_s3_key: str
    issue_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class ImplPrOpened(UsagePayload):
    """The Implementer has opened the single impl PR for this run.

    Marks the transition from ``implementer_running`` → ``impl_pr_open``.
    The state router dispatches the three validators (reviewer, tester,
    code-critic) next.
    """

    project_slug: str
    pr_url: Annotated[
        str,
        Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
    ]
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str


class ImplIterationRequested(Payload):
    """A signal arrived on the impl PR that should trigger an implementer revision.

    Emitted by the dashboard webhook handler on:

    * ``@aidlc-bot`` mention in an issue comment, PR review, or review comment
      on the impl PR (uncapped — the human is actively steering).

    Carries the mention text or feedback as a free-form string the implementer
    uses as input to its next revision. The event_projector appends
    ``delivery_id`` to the run row's ``delivery_ids`` set for idempotency.

    ``comment_id`` is populated for ``issue_comment_mention`` and
    ``review_comment_mention``; ``review_id`` is populated for
    ``review_changes_requested``. Both are nullable on the envelope so
    the schema is permissive, but the projector drops the feedback item
    when the id required by the discriminator is missing.
    """

    project_slug: str
    pr_url: Annotated[
        str,
        Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
    ]
    delivery_id: Annotated[str, Field(min_length=1, max_length=128)]
    source: Literal[
        "issue_comment_mention",
        "review_comment_mention",
        "review_changes_requested",
    ]
    commenter: Annotated[str, Field(min_length=1, max_length=128)]
    feedback_body: Annotated[str, Field(min_length=1, max_length=8192)]
    comment_id: Annotated[int, Field(ge=1)] | None = None
    review_id: Annotated[int, Field(ge=1)] | None = None


class ChecksPassed(Payload):
    """All required GitHub Checks for the impl PR's HEAD sha are conclusion=success.

    Emitted by the dashboard webhook handler after aggregating
    ``check_suite`` / ``check_run`` / ``workflow_run`` events for the
    PR's current HEAD sha. Advances the run to ``awaiting_human_merge``
    (or directly transitions ``validation_complete`` → ``awaiting_human_merge``
    when validation finished before CI did).
    """

    project_slug: str
    pr_url: Annotated[
        str,
        Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
    ]
    head_sha: Annotated[str, Field(min_length=7, max_length=40)]
    delivery_id: Annotated[str, Field(min_length=1, max_length=128)]


class ChecksFailed(Payload):
    """One or more required GitHub Checks for the impl PR's HEAD sha did not succeed.

    Emitted by the dashboard webhook handler after aggregating Checks
    events for the current HEAD sha. Triggers an implementer revision
    (counted toward ``MAX_REVISIONS``); the failing workflow names +
    URLs are passed to the implementer as :class:`common.runtime.CiFailureFeedback`.
    """

    project_slug: str
    pr_url: Annotated[
        str,
        Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
    ]
    head_sha: Annotated[str, Field(min_length=7, max_length=40)]
    delivery_id: Annotated[str, Field(min_length=1, max_length=128)]
    failed_workflow_count: Annotated[int, Field(ge=1)]
    summary: Annotated[str, Field(max_length=2048)]


class RunCancelRequested(Payload):
    """A user or system requested cancellation of an in-flight run.

    Emitted by the dashboard webhook on ``issues.unassigned`` (the bot
    was unassigned from the source issue), ``issues.closed``, or
    ``pull_request.closed`` with ``merged=false`` on the impl PR; also
    by the state-router when triage decides to ``ask`` / ``defer`` /
    ``decline``.
    """

    project_slug: str
    requestor: Annotated[str, Field(min_length=1, max_length=128)]
    source: Literal[
        "issue_unassigned",
        "issue_closed",
        "comment_command",
        "dashboard",
        "pr_closed",
    ]
    reason: Annotated[str, Field(max_length=512)] | None = None


class ReviewReady(UsagePayload):
    """Reviewer agent code-reviewed the unified impl PR — gating.

    Reviewer runs once per validation pass, against the integrated impl
    PR. Its ``verdict`` drives the run-level state machine: ``approve`` /
    ``comment`` advances the run to ``awaiting_checks`` (then
    ``awaiting_human_merge`` once Checks are green); ``request_changes``
    triggers a revision pass by the implementer (capped at ``MAX_REVISIONS``).
    Comments land on the impl PR via ``repo_helper.comment_pr``.
    """

    project_slug: str
    pr_url: str
    verdict: ReviewVerdict
    comment_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class TestReportReady(UsagePayload):
    """Tester agent identified test gaps on the unified impl PR — advisory.

    Tester runs once per validation pass, against the integrated impl
    PR. Gap suggestions land on the impl PR via ``repo_helper.comment_pr``;
    this event records counts and a summary for the dashboard. Does not
    gate the pipeline — its findings inform the reviewer's verdict and
    the implementer's revision pass (if triggered).
    """

    __test__ = False  # opt out of pytest collection (Test*-prefix collision)

    project_slug: str
    pr_url: str
    gap_count: Annotated[int, Field(ge=0)]
    suggested_test_count: Annotated[int, Field(ge=0)]
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class CodeCritiqueReady(UsagePayload):
    """Code-critic adversarially reviewed the unified impl PR — advisory.

    Code-critic runs once per validation pass, in parallel with
    reviewer + tester. Its findings (logical gaps, missing edge cases,
    drift from the plan's intent) land on the impl PR as comments and
    inform the reviewer's verdict + a revision pass if one is triggered.
    Does not gate the pipeline.
    """

    project_slug: str
    pr_url: str
    critique_s3_key: str
    issue_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class RevisionReady(UsagePayload):
    """Implementer revised the impl branch in response to feedback.

    Emitted after the implementer runs in ``mode=revision`` — clones
    the repo, checks out the impl branch, applies the aggregated
    reviewer + tester + code-critic feedback (and any CI failure or
    human-mention context), commits directly to the impl branch (no
    task branch), pushes. Advances the run from ``revising`` back to
    ``validation_running`` so the validators re-evaluate the updated diff.
    """

    project_slug: str
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    revision_number: Annotated[int, Field(ge=1)]
    session_id: str


class RunCompleted(Payload):
    """The run reached its terminal success state.

    Run-level usage totals (token_in/out, cost_usd, duration_ms) are
    accumulated on the run's STATE row by :mod:`event_projector` from
    each ``*.READY`` event's :class:`UsagePayload`; readers of cumulative
    metrics query the STATE row directly, not this event.
    """

    project_slug: str
    pr_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
        ]
        | None
    ) = None


class RunFailed(Payload):
    """The run reached a terminal failure state.

    ``revision_count`` is populated when the failure is the validator
    revision cap (``error_class="RevisionCapReached"``). The retrospector
    dispatcher uses it to reconstruct the per-round validator artifact
    S3 keys (``runs/{run_id}/validation/{reviewer,tester,code_critic}-r{N}.md``)
    so the retrospector can read every revision's findings and propose
    prompt / ``MEMORY.md`` updates that would have prevented the failure.
    """

    project_slug: str
    failed_state: str
    error_class: str
    error_message: str
    retryable: bool
    pr_url: Annotated[str, Field(max_length=512)] = ""
    source_issue_url: Annotated[str, Field(max_length=512)] = ""
    revision_count: Annotated[int, Field(ge=0, le=16)] = 0


type AnyPayload = (
    RequestReceived
    | IssueTriaged
    | DesignReady
    | CritiqueReady
    | ImplPrOpened
    | ImplIterationRequested
    | ChecksPassed
    | ChecksFailed
    | ReviewReady
    | TestReportReady
    | CodeCritiqueReady
    | RevisionReady
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
