"""Helpers for the AgentCore Runtime contract.

Each agent serves HTTP on ``:8080`` and exposes ``POST /invocations`` and
``GET /ping``. The ``bedrock-agentcore`` SDK ships :class:`BedrockAgentCoreApp`
that handles the contract for us — we just supply an entrypoint coroutine.

This module collects the small shared scaffolding — input/output models —
so each agent's ``app.py`` stays under 80 lines.

The pipeline is single-PR-per-issue:

  * The **Architect** receives an :class:`ArchitectInput` (intent +
    optional triggering comment) and returns an :class:`ArchitectResult`
    (plan_s3_key + summary). Plan is a single markdown document.
  * The **Implementer** is invoked once in ``mode=implementation`` and
    receives an :class:`ImplementerInput` (plan_s3_key + source issue
    refs), returning an :class:`ImplementerResult` (pr_url +
    diff_summary). On reviewer/CI/mention feedback it runs again in
    ``mode=revision``.
  * **Reviewer**, **Tester**, **Code-Critic** run in parallel against
    the impl PR. Code-Critic specifically reviews the implementation
    against the **original GitHub issue**, so its input includes the
    issue title + body + URL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from common.pricing import calculate_cost
from common.validators import NoneSafeList

logger = structlog.get_logger()


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

    The Architect produces a single ``plan.md`` document at
    ``s3://artifacts/runs/{run_id}/plan.md`` structured like a
    Claude Code plan-mode plan: Context, Assumptions, Approach, Files,
    Reuse, Implementation steps, Verification, Out of scope.

    ``triggering_comment_body`` carries the user's free-text guidance
    when the run was minted by an issue comment (``@aidlc-bot <text>``),
    with the bot mention stripped.

    ``source_issue_url``/``source_issue_title``/``source_issue_body``
    are the GitHub issue context the run was minted from. The Architect
    reads these to ground its plan.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    triggering_comment_body: Annotated[str | None, Field(default=None, max_length=8192)] = None
    requestor_sub: str | None = None
    target_repo: str | None = None
    source_issue_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512),
        ]
        | None
    ) = None
    source_issue_title: Annotated[str, Field(max_length=512)] | None = None
    source_issue_body: Annotated[str, Field(max_length=16384)] | None = None


class ArchitectResult(_UsageMixin):
    """Result the Architect returns. Becomes the DESIGN.READY payload."""

    plan_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    summary: Annotated[str, Field(max_length=2048)]
    proposed_adrs: NoneSafeList[str] = Field(default_factory=list)
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
    """Input passed to the Implementer's ``/invocations`` endpoint.

    The implementer runs in one of two modes:

    * ``mode="implementation"`` — first run for a run_id. Reads
      ``plan_s3_key`` from S3, executes the whole issue on a single
      branch ``aidlc/impl/{run_id}``, opens the impl PR, emits
      ``IMPL_PR.OPENED``.
    * ``mode="revision"`` — subsequent runs. Reads validator artifacts
      (and any CI failure / human-mention context) from S3, applies
      fixes directly on the impl branch, emits ``REVISION.READY``.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    mode: Literal["implementation", "revision"] = "implementation"
    plan_s3_key: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    revision_number: Annotated[int, Field(ge=0, le=16)] = 0
    # Set by the state_router on revision dispatches so the implementer
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
    # Aggregated feedback items the implementer should address on a
    # revision pass. Populated by the state router from CI failures,
    # human @-mentions, and changes_requested reviews.
    revision_feedback: Annotated[list[FeedbackItem], Field(max_length=32)] | None = None
    requestor_sub: str | None = None
    target_repo: (
        Annotated[
            str,
            Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$"),
        ]
        | None
    ) = None
    # Provenance: implementer writes ``Closes <url>`` in the PR body so
    # merging auto-closes the originating issue.
    source_issue_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512),
        ]
        | None
    ) = None
    # Used as the primary PR title for issue-driven runs and the fallback
    # summary line in the PR body when the agent's ``finish`` report omits one.
    source_issue_title: Annotated[str, Field(max_length=512)] | None = None
    # Free-text prompt from the dashboard form (or the issue title for
    # issue-driven runs). Used as the last-resort PR title fallback.
    intent: Annotated[str, Field(max_length=4096)] | None = None


class ImplementerResult(_UsageMixin):
    """Result the Implementer returns from ``mode=implementation`` runs.

    The implementer opens the unified impl PR (a single PR for the
    whole run) and reports its URL.
    """

    pr_url: Annotated[
        str,
        Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+/pull/\d+$"),
    ]
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str


class ImplementerRevisionResult(_UsageMixin):
    """Result the Implementer returns from ``mode=revision`` runs.

    Revision mode applies aggregated reviewer + tester + code-critic
    feedback (plus any CI failures or human-mention context) directly
    onto the impl branch. The runtime emits ``REVISION.READY`` and the
    state-router sends the run back into the validation pass.
    """

    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    revision_number: Annotated[int, Field(ge=1)]
    session_id: str


class ReviewerInput(_Frozen):
    """Input passed to the Reviewer's ``/invocations`` endpoint.

    Targets the unified impl PR. Runs in parallel with the tester and
    the code-critic. ``revision_number`` is 0 for the first validation
    pass and increments each time the reviewer requests changes and
    the implementer revises.

    The ``source_issue_*`` fields carry the original GitHub issue so
    the reviewer can adversarially check the architect's load-bearing
    assumptions against the issue's actual text (the architect can
    reinterpret an ambiguous requirement and have it slip through —
    the reviewer's job is to catch that).
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    plan_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    pr_url: str
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None
    revision_number: Annotated[int, Field(ge=0)] = 0
    source_issue_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512),
        ]
        | None
    ) = None
    source_issue_title: Annotated[str, Field(max_length=512)] | None = None
    source_issue_body: Annotated[str, Field(max_length=16384)] | None = None


class ReviewerResult(_UsageMixin):
    """Result the Reviewer returns. Becomes the REVIEW.READY payload."""

    pr_url: str
    verdict: Literal["approve", "request_changes", "comment"]
    comment_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class TesterInput(_Frozen):
    """Input passed to the Tester's ``/invocations`` endpoint.

    Targets the unified impl PR — runs in parallel with reviewer + code-critic.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    plan_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    pr_url: str
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None
    revision_number: Annotated[int, Field(ge=0)] = 0


class TesterResult(_UsageMixin):
    """Result the Tester returns. Becomes the TEST_REPORT.READY payload."""

    pr_url: str
    gap_count: Annotated[int, Field(ge=0)]
    suggested_test_count: Annotated[int, Field(ge=0)]
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class CodeCriticInput(_Frozen):
    """Input passed to the Code-Critic's ``/invocations`` endpoint.

    Targets the unified impl PR — runs in parallel with reviewer + tester.
    The Code-Critic specifically reviews **how well the implementation
    addresses the original GitHub issue** (not just the architect's
    plan). It receives the issue title + body + URL so it can compare
    the PR diff against the user's original ask.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    plan_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    pr_url: str
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None
    revision_number: Annotated[int, Field(ge=0)] = 0
    source_issue_url: (
        Annotated[
            str,
            Field(min_length=1, max_length=512),
        ]
        | None
    ) = None
    source_issue_title: Annotated[str, Field(max_length=512)] | None = None
    source_issue_body: Annotated[str, Field(max_length=16384)] | None = None


class CodeCriticResult(_UsageMixin):
    """Result the Code-Critic returns. Becomes the CODE_CRITIQUE.READY payload."""

    pr_url: str
    critique_s3_key: str
    issue_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class TriageInput(_Frozen):
    """Input passed to the Triage agent's ``/invocations`` endpoint.

    Built by the GitHub-issue webhook handler when the bot is assigned
    to an issue (and re-built when the issue receives a new comment
    while triage is awaiting an answer). The agent reads this payload,
    decides whether to ``proceed`` / ``ask`` / ``defer`` / ``decline`` /
    ``research``, and returns a :class:`TriageResult`.
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
    triggering_comment_body: Annotated[str | None, Field(default=None, max_length=8192)] = None
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    requestor_sub: str | None = None


class TriageResult(_Frozen):
    """Result the Triage agent returns. Becomes the ISSUE.TRIAGED payload.

    ``decision_s3_key`` points at the full :class:`common.triage.TriageDecision`
    JSON in S3; the flattened fields below are what downstream branches on
    without having to fetch the artifact. ``proceed`` runs the
    Architect → Critic → Implementer pipeline; ``research`` branches to
    the Proposer; ``ask`` / ``defer`` / ``decline`` terminate the run.
    """

    decision_s3_key: Annotated[str, Field(min_length=1, max_length=512)]
    action: Literal["proceed", "ask", "defer", "decline", "research"]
    rationale: Annotated[str, Field(min_length=1, max_length=2048)]
    missing_information_count: Annotated[int, Field(ge=0, le=8)] = 0
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    session_id: str


class ProposerInput(_Frozen):
    """Input passed to the Proposer's ``/invocations`` endpoint.

    The Proposer is invoked when triage classifies an issue as
    ``research``. :attr:`intent` carries the issue body and
    :attr:`issue_number` identifies the issue so the agent can read
    URLs from the body and post the synthesis as a comment back on
    that issue.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    base_branch: Annotated[str, Field(min_length=1, max_length=128)] = "main"
    trigger_reason: Literal["research"] = "research"
    intent: Annotated[str, Field(max_length=8192)] | None = None
    issue_number: Annotated[int, Field(ge=1)] | None = None
    triggering_comment_body: Annotated[str, Field(max_length=8192)] = ""
    triggering_commenter: Annotated[str, Field(max_length=64)] = ""
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


class RetrospectorInput(_Frozen):
    """Input passed to the Retrospector's ``/invocations`` endpoint.

    The dispatcher Lambda fires this in one of two modes:

    * ``mode="capture"`` — one invocation per PR-signal event
      (``IMPL_PR.OPENED``, ``REVIEW.READY``, ``CHECKS.PASSED``,
      ``CHECKS.FAILED``, ``IMPL.ITERATION_REQUESTED``) and per
      terminal event (``RUN.COMPLETED`` / ``RUN.FAILED`` /
      ``RUN.CANCEL_REQUESTED``). The agent reads the slice of the
      run relevant to the event and appends zero or more lesson
      bullets to the pending-lessons buffer in S3.
    * ``mode="consolidate"`` — fanned out by a scheduled rule
      (``SCHEDULED.LESSONS_CONSOLIDATE``) once per destination
      (``target_repo`` per active project, ``platform`` once). The
      agent reads the buffer, dedupes, picks the best bullets,
      opens ≤2 PRs (MEMORY.md + optional SKILL.md), and truncates
      the buffer of the bullets it shipped.

    On a cap-hit failure (``event_type="RUN.FAILED"`` with
    ``revision_count >= 3``), the dispatcher populates
    ``validation_artifact_keys`` with the S3 keys of every validator
    artifact written across the revision rounds so the retrospector
    can mine the recurring failure pattern for a high-value bullet.
    """

    mode: Literal["capture", "consolidate"] = "capture"
    event_type: Literal[
        "RUN.COMPLETED",
        "RUN.FAILED",
        "RUN.CANCEL_REQUESTED",
        "IMPL_PR.OPENED",
        "REVIEW.READY",
        "CHECKS.PASSED",
        "CHECKS.FAILED",
        "IMPL.ITERATION_REQUESTED",
        "SCHEDULED.LESSONS_CONSOLIDATE",
    ]
    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    destination: Literal["target_repo", "platform"] | None = None
    pr_url: Annotated[str, Field(max_length=512)] = ""
    issue_url: Annotated[str, Field(max_length=512)] = ""
    reason: Annotated[str, Field(max_length=2048)] = ""
    verdict: Literal["approve", "comment", "request_changes"] | None = None
    pr_comment_body: Annotated[str, Field(max_length=8192)] = ""
    revision_count: Annotated[int, Field(ge=0, le=16)] = 0
    validation_artifact_keys: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=512)]],
        Field(max_length=64),
    ] = Field(default_factory=list)
    run_id: str
    correlation_id: str
    actor_id: str = "system"


def default_retry_strategy(model_id: str) -> Any:
    """Return a Bedrock-throttling retry policy tuned for our model tier.

    Strands' default ``ModelRetryStrategy`` retries
    ``ModelThrottledException`` with 6 attempts and 4s→128s exponential
    backoff. Haiku tolerates fewer attempts and a tighter cap because it
    runs in higher-volume contexts (Triage, Tester) where a long backoff
    chain blocks the dispatch more than it helps. Opus and Sonnet keep
    the default — Opus throttles are stickier, so we want the full 6 attempts.

    The return type is :class:`Any` so this module stays importable from
    Implementer code that doesn't pull in Strands.

    Args:
        model_id: Bedrock model id (e.g., the value of
            ``AIDLC_BEDROCK_MODEL_ID``).

    Returns:
        A Strands ``ModelRetryStrategy`` ready to pass into
        ``Agent(retry_strategy=...)``.
    """
    from strands import ModelRetryStrategy  # noqa: PLC0415

    if "haiku" in model_id.lower():
        return ModelRetryStrategy(max_attempts=4, initial_delay=2, max_delay=30)
    return ModelRetryStrategy(max_attempts=6, initial_delay=4, max_delay=128)


def invoke_with_fallback[T](
    *,
    primary_model_id: str,
    fallback_model_id: str | None,
    build: Callable[[str], Any],
    run: Callable[[Any], T],
) -> tuple[Any, str, T]:
    """Run a Strands ``Agent`` with cross-model fallback on throttling.

    Builds the agent via ``build(primary_model_id)`` and invokes it via
    ``run(agent)``. If the call raises ``ModelThrottledException`` (i.e.,
    Bedrock kept throttling after the agent's ``retry_strategy``
    exhausted its attempts), and a distinct ``fallback_model_id`` is
    configured, a fresh agent is built with the fallback id and the
    workflow is re-invoked once. The fallback agent runs the work from
    scratch — any side effects the primary made before throttling are
    redone, so ``run`` must be idempotent at the artifact level (e.g.,
    overwriting the same S3 key is fine, opening a duplicate PR is not).

    When ``fallback_model_id`` is ``None``, empty, or equal to the
    primary id, no fallback is attempted and the throttle exception
    propagates.

    Args:
        primary_model_id: Bedrock model id to try first.
        fallback_model_id: Bedrock model id to retry with when the
            primary throttles, or ``None`` to disable fallback.
        build: ``build(model_id) -> Agent`` factory. Called once for
            the primary and again with ``fallback_model_id`` if the
            primary throttles.
        run: ``run(agent) -> T`` workflow. Invoked under the agent
            returned by ``build``; any return value is propagated.

    Returns:
        ``(agent, model_id_used, result)`` so the caller can read
        usage metrics off the agent and price them against the model
        that actually ran (the fallback if it kicked in).
    """
    from strands.types.exceptions import ModelThrottledException  # noqa: PLC0415

    agent = build(primary_model_id)
    try:
        result = run(agent)
    except ModelThrottledException as exc:
        if not fallback_model_id or fallback_model_id == primary_model_id:
            raise
        logger.warning(
            "primary bedrock model throttled — retrying with fallback",
            primary_model_id=primary_model_id,
            fallback_model_id=fallback_model_id,
            error=str(exc),
        )
        agent = build(fallback_model_id)
        return agent, fallback_model_id, run(agent)
    return agent, primary_model_id, result


def run_for_structured_output[T: BaseModel](
    agent: Any,
    *,
    output_model: type[T],
    prompt: str,
) -> T:
    """Run a Strands ``Agent`` and return its validated structured output.

    Wraps ``agent(prompt, structured_output_model=output_model)`` — the
    pattern Strands recommends for tool-using agents that need to emit a
    typed response. Crucially this runs the full agent loop with all
    configured tools available; the deprecated ``Agent.structured_output``
    method bypasses the tool registry entirely, so any grounding tools
    declared on the agent are unreachable on that path.

    The Strands ``AgentResult.structured_output`` is typed as
    ``BaseModel | None``. We narrow back to the requested type and raise
    when the model failed to produce structured output (e.g., max_tokens
    cut the response off mid-tool-call).

    The agent type is :class:`Any` so this module stays importable from
    Implementer code that does not depend on Strands.
    """
    result = agent(prompt, structured_output_model=output_model)
    output = result.structured_output
    if not isinstance(output, output_model):
        msg = f"agent did not produce a {output_model.__name__}"
        raise TypeError(msg)
    return output


def usage_from_strands(agent: Any, *, model_id: str) -> dict[str, Any]:
    """Extract token + cost + duration from a Strands ``Agent``.

    Strands' ``EventLoopMetrics.accumulated_usage`` accumulates input /
    output tokens across every model call the agent made during one
    ``__call__`` invocation; ``accumulated_metrics`` carries cumulative
    latency in ms. Cost is computed via the local pricing table since
    Strands does not compute it.

    Returns a dict ready to splat into a ``*Result`` constructor::

        result = ArchitectResult(
            plan_s3_key=...,
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
