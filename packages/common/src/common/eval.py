"""Pydantic contracts for the production efficiency-eval feedback loop.

The platform measures itself by what humans do with the PRs it opens:

  * Merged with no requested-changes cycle — high quality.
  * Merged after iteration — blind spots; the comment-category mix
    explains why.
  * Closed without merge — wrong direction.

Pipeline:

  1. ``pr_telemetry`` Lambda captures GitHub webhook events
     (``pull_request``, ``pull_request_review``, ``pull_request_review_comment``,
     ``issue_comment`` on PRs) into :class:`PRTelemetry` rows.
  2. ``comment_classifier`` Lambda categorises each review comment with
     a fast model into :class:`ClassifiedComment`.
  3. ``eval_aggregator`` rolls (1) + (2) into per-bucket
     :class:`EfficiencyMetrics` on a schedule, computes drift, and
     emits :class:`DriftSignal` events when the rolling score
     deteriorates against baseline.
  4. The Proposer subscribes to :class:`DriftSignal` and opens PRs
     against ``docs/MEMORY.md`` or agent prompts.

One-way door PRs are excluded from :attr:`EfficiencyMetrics.merge_as_is_rate`
(they always require human touch by design) and reported separately as
:attr:`EfficiencyMetrics.one_way_merge_rate`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.door import DoorClass
from common.events import Payload
from common.triage import WorkflowKind

CommentCategory = Literal[
    "nit",
    "bug",
    "design",
    "missing_test",
    "security",
    "performance",
    "documentation",
    "convention",
    "scope",
    "unclear",
]
"""Coarse categorisation of a single review comment.

Categories were chosen to map to ai-dlc agent ownership: ``design``
points back at Architect, ``missing_test`` at Tester, ``convention`` at
``MEMORY.md``, ``scope`` at task decomposition, etc. The Proposer reads
the dominant category to pick which artifact to edit.
"""

AgentOwner = Literal[
    "architect",
    "critic",
    "implementer",
    "reviewer",
    "tester",
    "proposer",
    "triage",
]
"""Which agent's output produced (or last touched) the artifact under review."""


COMMENT_WEIGHT: dict[CommentCategory, int] = {
    "nit": 0,
    "bug": 3,
    "design": 3,
    "missing_test": 2,
    "security": 5,
    "performance": 1,
    "documentation": 1,
    "convention": 1,
    "scope": 4,
    "unclear": 1,
}
"""Per-category weights summed into the friction score.

``nit`` is deliberately zero — non-blocking nits should not look like
quality regressions. ``security`` is heaviest because shipping insecure
code is the failure mode we most want to catch.
"""


class _Frozen(BaseModel):
    """Strict, frozen base for evaluation models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ClassifiedComment(_Frozen):
    """One review comment after categorisation by the classifier model."""

    pr_url: Annotated[str, Field(min_length=1, max_length=512)]
    comment_id: Annotated[int, Field(ge=1)]
    author: Annotated[str, Field(min_length=1, max_length=128)]
    is_bot: bool
    category: CommentCategory
    quoted: Annotated[str, Field(min_length=1, max_length=2048)]
    classified_at: datetime
    classifier_model_id: Annotated[str, Field(min_length=1, max_length=128)]


class PRTelemetry(_Frozen):
    """One PR's lifecycle, captured from GitHub webhook events.

    Updated as the PR progresses; final shape lands when the PR is
    merged or closed. Stored per-PR-URL in DynamoDB; rows are immutable
    once written — every webhook event produces a new row keyed by
    ``(pr_url, observed_at)``, and the aggregator coalesces.
    """

    pr_url: Annotated[str, Field(min_length=1, max_length=512)]
    target_repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    spec_slug: Annotated[str | None, Field(max_length=128)] = None
    task_id: Annotated[str | None, Field(max_length=32)] = None
    workflow_kind: WorkflowKind
    door_class: DoorClass
    agent_owner: AgentOwner
    prompt_variant: Annotated[str, Field(min_length=1, max_length=8)] = "a"
    opened_at: datetime
    opened_as_draft: bool = False
    marked_ready_at: datetime | None = None
    marked_ready_by: Annotated[str | None, Field(max_length=128)] = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    merged: bool = False
    requested_changes_count: Annotated[int, Field(ge=0)] = 0
    review_count: Annotated[int, Field(ge=0)] = 0
    comment_count_human: Annotated[int, Field(ge=0)] = 0
    comment_count_bot: Annotated[int, Field(ge=0)] = 0


class EfficiencyMetrics(_Frozen):
    """Aggregated efficiency for one ``(repo x agent x prompt_variant)`` bucket.

    Computed by the aggregator on a rolling window. ``merge_as_is_rate``
    is the headline; ``weighted_friction_score`` drives drift detection
    via the weights in :data:`COMMENT_WEIGHT`. Lower friction = better.
    """

    target_repo: Annotated[str, Field(min_length=3, max_length=128)]
    agent_owner: AgentOwner
    prompt_variant: Annotated[str, Field(min_length=1, max_length=8)]
    window_start: datetime
    window_end: datetime
    pr_count: Annotated[int, Field(ge=0)]
    merge_as_is_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    one_way_merge_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    weighted_friction_score: Annotated[float, Field(ge=0.0)]
    median_time_to_merge_hours: Annotated[float | None, Field(ge=0.0)] = None
    rejection_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    comments_by_category: dict[CommentCategory, int] = Field(default_factory=dict)


class DriftSignal(Payload):
    """Emitted when rolling efficiency drops against baseline.

    Threshold: ``delta_pct >= 20`` and ``sample_size >= 10`` (the
    aggregator's ``DRIFT_DELTA_PCT`` / ``DRIFT_MIN_SAMPLE_SIZE``).
    The Proposer subscribes to this event and is restricted by its
    Pydantic validator to opening PRs against prompts and
    ``docs/MEMORY.md`` only.

    Inherits from :class:`common.events.Payload` so it slots into the
    ``EventEnvelope[DriftSignal]`` envelope as the payload of
    ``EVAL.DRIFT_DETECTED``.
    """

    target_repo: Annotated[str, Field(min_length=3, max_length=128)]
    agent_owner: AgentOwner
    prompt_variant: Annotated[str, Field(min_length=1, max_length=8)]
    detected_at: datetime
    rolling_window_score: Annotated[float, Field(ge=0.0)]
    baseline_score: Annotated[float, Field(ge=0.0)]
    delta_pct: Annotated[float, Field(ge=0.0)]
    sample_size: Annotated[int, Field(ge=10)]
    dominant_category: CommentCategory
