"""Pure-function aggregation helpers — kept separate from the Lambda glue.

Inputs are plain :class:`PRTelemetry` rows + classified-comment counts;
output is :class:`EfficiencyMetrics` per ``(target_repo, agent_owner,
prompt_variant)`` bucket. The aggregator is deterministic; the Lambda
just feeds it data from DDB / S3 and emits events for the results.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

from common.eval import (
    COMMENT_WEIGHT,
    AgentOwner,
    CommentCategory,
    EfficiencyMetrics,
    PRTelemetry,
)

if TYPE_CHECKING:
    pass


BucketKey = tuple[str, AgentOwner, str]
"""``(target_repo, agent_owner, prompt_variant)`` aggregation grain.

Per-repo so conventions don't bleed across projects, per-agent so the
proposer can target a specific agent's prompt, per-prompt-variant so
A/B comparisons compose at the same grain.
"""


def bucket_key(row: PRTelemetry) -> BucketKey:
    """Per-row aggregation key."""
    return (row.target_repo, row.agent_owner, row.prompt_variant)


def merge_as_is(rows: Iterable[PRTelemetry]) -> tuple[int, int]:
    """Count merged-as-is PRs (zero requested-changes cycles, excludes ONE-WAY).

    Returns ``(merged_clean, total_excluding_one_way)``. Commitment C1
    excludes ``door_class="one_way"`` from the headline rate because
    ONE-WAY PRs always have human touch by design.
    """
    merged_clean = 0
    total = 0
    for row in rows:
        if row.door_class == "one_way":
            continue
        total += 1
        if row.merged and row.requested_changes_count == 0:
            merged_clean += 1
    return merged_clean, total


def one_way_merge_rate(rows: Iterable[PRTelemetry]) -> tuple[int, int]:
    """Count merged ONE-WAY PRs / total ONE-WAY PRs in the window."""
    merged = 0
    total = 0
    for row in rows:
        if row.door_class != "one_way":
            continue
        total += 1
        if row.merged:
            merged += 1
    return merged, total


def rejection_rate(rows: Iterable[PRTelemetry]) -> tuple[int, int]:
    """Count PRs closed without merge / total closed PRs in the window."""
    rejected = 0
    closed = 0
    for row in rows:
        if row.closed_at is None:
            continue
        closed += 1
        if not row.merged:
            rejected += 1
    return rejected, closed


def weighted_friction_score(comments_by_category: dict[CommentCategory, int]) -> float:
    """Sum the per-category weights from :data:`common.eval.COMMENT_WEIGHT`.

    Lower score = lower friction = higher quality. Weights:
    ``nit=0, bug=3, design=3, missing_test=2, security=5, scope=4`` etc.
    """
    return float(sum(COMMENT_WEIGHT[cat] * count for cat, count in comments_by_category.items()))


def median_time_to_merge_hours(rows: Iterable[PRTelemetry]) -> float | None:
    """Median open-to-merge duration in hours; ``None`` when no merged PRs."""
    deltas = sorted(
        (row.merged_at - row.opened_at).total_seconds() / 3600.0
        for row in rows
        if row.merged and row.merged_at is not None
    )
    if not deltas:
        return None
    mid = len(deltas) // 2
    if len(deltas) % 2 == 1:
        return deltas[mid]
    return (deltas[mid - 1] + deltas[mid]) / 2.0


def safe_rate(numerator: int, denominator: int) -> float:
    """Float division that returns ``0.0`` when there's no data."""
    if denominator == 0:
        return 0.0
    return numerator / denominator


def aggregate(
    rows: list[PRTelemetry],
    *,
    comments: dict[BucketKey, dict[CommentCategory, int]],
    window_start: datetime,
    window_end: datetime,
) -> list[EfficiencyMetrics]:
    """Roll one window's rows into per-bucket metrics."""
    by_bucket: dict[BucketKey, list[PRTelemetry]] = defaultdict(list)
    for row in rows:
        by_bucket[bucket_key(row)].append(row)

    out: list[EfficiencyMetrics] = []
    for key, bucket_rows in by_bucket.items():
        target_repo, agent_owner, prompt_variant = key
        merged_clean, two_way_total = merge_as_is(bucket_rows)
        ow_merged, ow_total = one_way_merge_rate(bucket_rows)
        rejected, closed = rejection_rate(bucket_rows)
        bucket_comments = comments.get(key, {})
        out.append(
            EfficiencyMetrics(
                target_repo=target_repo,
                agent_owner=agent_owner,
                prompt_variant=prompt_variant,
                window_start=window_start,
                window_end=window_end,
                pr_count=len(bucket_rows),
                merge_as_is_rate=safe_rate(merged_clean, two_way_total),
                one_way_merge_rate=safe_rate(ow_merged, ow_total),
                weighted_friction_score=weighted_friction_score(bucket_comments),
                median_time_to_merge_hours=median_time_to_merge_hours(bucket_rows),
                rejection_rate=safe_rate(rejected, closed),
                comments_by_category=bucket_comments,
            ),
        )
    return out


# Drift fires only when the rolling friction score is materially worse
# than baseline AND the sample is large enough to trust.
DRIFT_DELTA_PCT = 20.0
DRIFT_MIN_SAMPLE_SIZE = 10


def drift_detected(
    *,
    rolling_score: float,
    baseline_score: float,
    sample_size: int,
) -> bool:
    """Apply the C4 drift rule: ≥20% friction increase AND ≥10 PRs in window."""
    if sample_size < DRIFT_MIN_SAMPLE_SIZE:
        return False
    if baseline_score <= 0.0:
        return rolling_score > 0.0  # nothing to compare against; any friction is drift
    delta_pct = ((rolling_score - baseline_score) / baseline_score) * 100.0
    return delta_pct >= DRIFT_DELTA_PCT


def drift_delta_pct(*, rolling_score: float, baseline_score: float) -> float:
    """Percentage delta of rolling vs baseline (positive = more friction)."""
    if baseline_score <= 0.0:
        return 100.0 if rolling_score > 0.0 else 0.0
    return ((rolling_score - baseline_score) / baseline_score) * 100.0


def dominant_category(comments: dict[CommentCategory, int]) -> CommentCategory:
    """Pick the category with the highest weighted contribution."""
    if not comments:
        return "unclear"
    return max(comments.items(), key=lambda kv: COMMENT_WEIGHT[kv[0]] * kv[1])[0]
