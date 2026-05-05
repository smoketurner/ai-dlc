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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    """Strict, frozen base for the runtime contract types."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


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


class ArchitectResult(_Frozen):
    """Result the Architect returns. Becomes the SPEC.READY payload."""

    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    requirements_summary: Annotated[str, Field(max_length=1024)]
    design_summary: Annotated[str, Field(max_length=1024)]
    task_count: Annotated[int, Field(ge=1)]
    task_ids: Annotated[list[str], Field(min_length=1, max_length=64)]
    proposed_adrs: list[str] = Field(default_factory=list)
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


class CriticResult(_Frozen):
    """Result the Critic returns. Becomes the CRITIQUE.READY payload."""

    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    critique_s3_key: str
    issue_count: Annotated[int, Field(ge=0)]
    high_severity_count: Annotated[int, Field(ge=0)] = 0
    medium_severity_count: Annotated[int, Field(ge=0)] = 0
    low_severity_count: Annotated[int, Field(ge=0)] = 0
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class ImplementerInput(_Frozen):
    """Input passed to the Implementer's ``/invocations`` endpoint, per task.

    ``target_repo`` (``owner/name``) is required for the Implementer — it's
    the repo the Implementer clones, commits to, and opens a PR on. When
    ``requestor_sub`` is set, the Implementer fetches that user's GitHub
    OAuth token via AgentCore Identity and configures git author identity
    accordingly so commits attribute to the requestor.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    prior_feedback: str | None = None
    requestor_sub: str | None = None
    target_repo: (
        Annotated[
            str,
            Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$"),
        ]
        | None
    ) = None
    # Set by the runtime_invoker shim so the agent can call
    # ``states:SendTaskSuccess`` / ``SendTaskFailure`` directly when
    # done. Sessions longer than the SDK's HTTP read timeout (~60s) need
    # this; the synchronous /invocations response body is ignored when
    # ``task_token`` is set.
    task_token: str | None = None


class ImplementerResult(_Frozen):
    """Result the Implementer returns. Becomes the TASK.READY payload."""

    task_id: str
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str


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
    task_token: str | None = None


class ReviewerResult(_Frozen):
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
    task_token: str | None = None


class TesterResult(_Frozen):
    """Result the Tester returns. Becomes the TEST_REPORT.READY payload."""

    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    pr_url: str
    gap_count: Annotated[int, Field(ge=0)]
    suggested_test_count: Annotated[int, Field(ge=0)]
    summary: Annotated[str, Field(max_length=2048)]
    session_id: str


class ProposerInput(_Frozen):
    """Input passed to the Proposer's ``/invocations`` endpoint.

    The Proposer runs out of the main SDLC pipeline — it's invoked by an
    EventBridge schedule (weekly) and on regression detection from 9b.
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
