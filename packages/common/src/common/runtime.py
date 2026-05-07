"""Helpers for the AgentCore Runtime contract.

Each agent serves HTTP on ``:8080`` and exposes ``POST /invocations`` and
``GET /ping``. The ``bedrock-agentcore`` SDK ships :class:`BedrockAgentCoreApp`
that handles the contract for us — we just supply an entrypoint coroutine.

This module collects the small shared scaffolding — input/output models —
so each agent's ``app.py`` stays under 80 lines.

The pipeline is spec-driven:

  * The **Architect** receives an :class:`ArchitectInput` (intent + retry
    feedback) and returns an :class:`ArchitectResult` (spec_s3_prefix +
    summaries + task count).
  * The **Critic** receives a :class:`CriticInput` (spec_s3_prefix + intent)
    and returns a :class:`CriticResult` (critique_s3_key + severity counts).
    Advisory only — does not gate the pipeline.
  * The **Implementer** is invoked once per task and receives an
    :class:`ImplementerInput` (spec_slug + task_id + retry feedback),
    returning an :class:`ImplementerResult` (pr_url + diff_summary).
  * The **Reviewer** receives a :class:`ReviewerInput` (pr_url + diff_summary
    + spec context) and returns a :class:`ReviewerResult` (verdict + comment
    counts + severity). Advisory only.
  * The **Tester** receives a :class:`TesterInput` (pr_url + diff_summary)
    and returns a :class:`TesterResult` (gap counts + suggested test count).
    Advisory only.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.pricing import calculate_cost
from common.validators import NoneSafeList


class _Frozen(BaseModel):
    """Strict, frozen base for the runtime contract types."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class _UsageMixin(_Frozen):
    """Per-invocation usage fields shared by every agent's result.

    Each agent populates these from its framework's metrics:

    * Strands agents read ``Agent.event_loop_metrics.accumulated_usage`` /
      ``accumulated_metrics["latencyMs"]`` after the call returns and
      compute cost via :mod:`common.pricing`.
    * The Implementer reads :class:`claude_agent_sdk.ResultMessage` —
      it ships ``usage``, ``total_cost_usd``, and ``duration_ms`` directly.

    Defaults are zero so existing agents that haven't been wired yet
    keep validating; the dashboard simply shows ``0`` until they emit.
    """

    token_in: Annotated[int, Field(ge=0)] = 0
    token_out: Annotated[int, Field(ge=0)] = 0
    cost_usd: Annotated[float, Field(ge=0.0)] = 0.0
    duration_ms: Annotated[int, Field(ge=0)] = 0


class ArchitectInput(_Frozen):
    """Input passed to the Architect's ``/invocations`` endpoint.

    Step Functions sends this body when invoking the architect runtime,
    populating ``prior_feedback`` if this is a retry after rejection.
    ``requestor_sub`` and ``target_repo`` are threaded through every agent's
    input so downstream agents (Implementer, Reviewer, Tester) can act on
    behalf of the user against the right repo.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    prior_feedback: str | None = None
    requestor_sub: str | None = None
    target_repo: str | None = None


class ArchitectResult(_UsageMixin):
    """Result the Architect returns. Becomes the SPEC.READY payload.

    ``one_way_task_count`` is the number of tasks the Architect classified
    as one-way doors. Step Functions reads it (along with the Critic's
    ``high_severity_count``) to decide whether the spec gate can auto-approve
    or has to wait for a human.
    """

    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    requirements_summary: Annotated[str, Field(max_length=1024)]
    design_summary: Annotated[str, Field(max_length=1024)]
    task_count: Annotated[int, Field(ge=1)]
    task_ids: Annotated[list[str], Field(min_length=1, max_length=64)]
    one_way_task_count: Annotated[int, Field(ge=0)] = 0
    proposed_adrs: NoneSafeList[str] = Field(default_factory=list)
    session_id: str


class CriticInput(_Frozen):
    """Input passed to the Critic's ``/invocations`` endpoint.

    Step Functions sends this body after the Architect produces a spec; the
    Critic reads the spec from S3 and emits an adversarial review.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None
    target_repo: str | None = None


class CriticResult(_UsageMixin):
    """Result the Critic returns. Becomes the CRITIQUE.READY payload."""

    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    critique_s3_key: str
    issue_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class CiFailureFeedback(_Frozen):
    """One failed CI workflow run, fed to the implementer on iteration."""

    kind: Literal["ci_failure"] = "ci_failure"
    workflow_name: Annotated[str, Field(min_length=1, max_length=256)]
    conclusion: Literal["failure", "timed_out", "cancelled", "action_required", "stale"]
    head_sha: Annotated[str, Field(min_length=7, max_length=40)]
    html_url: Annotated[str, Field(max_length=512)]


class ReviewChangesRequestedFeedback(_Frozen):
    """A reviewer submitted a changes_requested review on the PR."""

    kind: Literal["review_changes_requested"] = "review_changes_requested"
    reviewer: Annotated[str, Field(min_length=1, max_length=128)]
    body: Annotated[str, Field(max_length=8192)] = ""
    review_id: Annotated[int, Field(ge=1)]


class ReviewCommentMentionFeedback(_Frozen):
    """A line-anchored PR review comment that @-mentions the bot."""

    kind: Literal["review_comment_mention"] = "review_comment_mention"
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    line: int | None = None
    commit_id: Annotated[str, Field(min_length=7, max_length=40)]
    comment_id: Annotated[int, Field(ge=1)]
    in_reply_to_id: int | None = None
    body: Annotated[str, Field(max_length=8192)]
    commenter: Annotated[str, Field(min_length=1, max_length=128)]


class IssueCommentMentionFeedback(_Frozen):
    """A PR-conversation comment that @-mentions the bot."""

    kind: Literal["issue_comment_mention"] = "issue_comment_mention"
    comment_id: Annotated[int, Field(ge=1)]
    body: Annotated[str, Field(max_length=8192)]
    commenter: Annotated[str, Field(min_length=1, max_length=128)]


type FeedbackItem = Annotated[
    CiFailureFeedback
    | ReviewChangesRequestedFeedback
    | ReviewCommentMentionFeedback
    | IssueCommentMentionFeedback,
    Field(discriminator="kind"),
]


class ImplementerInput(_Frozen):
    """Input passed to the Implementer's ``/invocations`` endpoint, per task.

    ``target_repo`` (``owner/name``) is required for the Implementer — it's
    the repo the Implementer clones, commits to, and opens a PR on. When
    ``requestor_sub`` is set, the Implementer fetches that user's GitHub
    OAuth token via AgentCore Identity and configures git author identity
    accordingly so commits attribute to the requestor.

    On iteration runs (``iteration_count > 0``) the state-router fills
    ``iteration_feedback`` and ``pr_url`` so the implementer pushes a fix
    commit on the existing PR branch rather than starting from ``main``.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    iteration_count: Annotated[int, Field(ge=0, le=16)] = 0
    iteration_feedback: Annotated[list[FeedbackItem], Field(max_length=32)] | None = None
    # Set by the state_router on iteration dispatches so the implementer
    # can post inline replies + status updates against the existing PR.
    # ``None`` on the first dispatch (PR doesn't exist yet — implementer
    # opens it).
    pr_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
        ]
        | None
    ) = None
    requestor_sub: str | None = None
    target_repo: (
        Annotated[
            str,
            Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$"),
        ]
        | None
    ) = None


class ImplementerResult(_UsageMixin):
    """Result the Implementer returns. Becomes the TASK.READY payload.

    ``pr_url`` is ``None`` when the agent reported ``status='blocked'`` via
    the ``finish`` tool — no PR was opened. ``blocked_reason`` carries the
    agent's explanation in that case.
    """

    task_id: str
    pr_url: str | None = None
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str
    blocked_reason: Annotated[str, Field(max_length=512)] | None = None


class ReviewerInput(_Frozen):
    """Input passed to the Reviewer's ``/invocations`` endpoint, per task PR."""

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None


class ReviewerResult(_UsageMixin):
    """Result the Reviewer returns. Becomes the REVIEW.READY payload."""

    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    verdict: Literal["approve", "request_changes", "comment"]
    comment_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class TesterInput(_Frozen):
    """Input passed to the Tester's ``/invocations`` endpoint, per task PR."""

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None


class TesterResult(_UsageMixin):
    """Result the Tester returns. Becomes the TEST_REPORT.READY payload."""

    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    gap_count: Annotated[int, Field(ge=0)]
    suggested_test_count: Annotated[int, Field(ge=0)]
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class TriageInput(_Frozen):
    """Input passed to the Triage agent's ``/invocations`` endpoint.

    Built by the GitHub-issue webhook handler when the bot is assigned
    to an issue (and re-built when the issue receives a new comment
    while triage is awaiting an answer). The agent reads this payload,
    decides whether to ``proceed`` / ``ask`` / ``defer`` / ``decline``,
    and returns a :class:`TriageResult` carrying the structured decision.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    issue_url: Annotated[str, Field(min_length=1, max_length=512)]
    issue_number: Annotated[int, Field(ge=1)]
    issue_title: Annotated[str, Field(min_length=1, max_length=512)]
    issue_body: Annotated[str, Field(max_length=8192)]
    issue_type: Literal["Bug", "Feature", "Task", "Other"] | None = None
    issue_labels: Annotated[NoneSafeList[str], Field(max_length=32)] = Field(
        default_factory=list,
    )
    prior_triage_count: Annotated[int, Field(ge=0, le=16)] = 0
    prior_human_comments: Annotated[
        NoneSafeList[Annotated[str, Field(min_length=1, max_length=2048)]],
        Field(max_length=16),
    ] = Field(default_factory=list)
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None


class TriageResult(_Frozen):
    """Result the Triage agent returns. Becomes the ISSUE.TRIAGED payload.

    ``decision_s3_key`` points at the full :class:`common.triage.TriageDecision`
    JSON in S3; the flattened fields below are what the Step Functions
    ``Choice`` state branches on without having to fetch the artifact.
    ``workflow_kind`` is set only when ``action == "proceed"``;
    ``missing_information_count`` is non-zero only when ``action == "ask"``.
    """

    decision_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    action: Literal["proceed", "ask", "defer", "decline"]
    workflow_kind: Literal["spec_driven", "bug_fix", "upgrade", "docs"] | None = None
    rationale: Annotated[str, Field(min_length=1, max_length=2048)]
    missing_information_count: Annotated[int, Field(ge=0, le=8)] = 0
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    session_id: str


class ProposerInput(_Frozen):
    """Input passed to the Proposer's ``/invocations`` endpoint.

    The Proposer runs out of the main SDLC pipeline — it's invoked by an
    EventBridge schedule (weekly) and on alerts from the eval-regression
    drift detector.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    base_branch: Annotated[str, Field(min_length=1, max_length=128)] = "main"
    trigger_reason: Literal["scheduled", "regression"] = "scheduled"
    evals_lookback_days: Annotated[int, Field(ge=1, le=365)] = 30
    run_id: str
    correlation_id: str
    actor_id: str = "system"


class ProposerResult(_Frozen):
    """Result the Proposer returns.

    ``proposal_made=False`` indicates the Proposer judged the signals
    insufficient to warrant a change — no PR opened.
    """

    proposal_made: bool
    pr_url: str | None = None
    target_files: list[str] = []
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


def usage_from_strands(agent: Any, *, model_id: str) -> dict[str, Any]:
    """Extract token + cost + duration from a Strands ``Agent``.

    Strands' ``EventLoopMetrics.accumulated_usage`` accumulates input /
    output tokens across every model call the agent made during one
    ``__call__`` / ``structured_output`` invocation; ``accumulated_metrics``
    carries cumulative latency in ms. Cost is computed via the local
    pricing table since Strands does not compute it.

    Returns a dict ready to splat into a ``*Result`` constructor::

        result = ArchitectResult(
            spec_slug=...,
            **usage_from_strands(agent, model_id=...),
        )

    Defensive: returns zeros for all fields when ``event_loop_metrics``
    is missing or doesn't carry the expected keys (e.g., a stubbed agent
    in tests).
    """
    metrics = getattr(agent, "event_loop_metrics", None)
    if metrics is None:
        return {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 0}
    usage = getattr(metrics, "accumulated_usage", None) or {}
    perf = getattr(metrics, "accumulated_metrics", None) or {}
    in_tokens = int(usage.get("inputTokens", 0) or 0)
    out_tokens = int(usage.get("outputTokens", 0) or 0)
    return {
        "token_in": in_tokens,
        "token_out": out_tokens,
        "cost_usd": calculate_cost(
            model_id,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        ),
        "duration_ms": int(perf.get("latencyMs", 0) or 0),
    }
