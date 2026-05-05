"""Tests for the pure-function aggregator helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from common.eval import PRTelemetry
from eval_aggregator.aggregate import (
    aggregate,
    bucket_key,
    drift_delta_pct,
    drift_detected,
    median_time_to_merge_hours,
    merge_as_is,
    one_way_merge_rate,
    rejection_rate,
    safe_rate,
    weighted_friction_score,
)


def now() -> datetime:
    return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def telemetry(**overrides: Any) -> PRTelemetry:
    base: dict[str, Any] = {
        "pr_url": "https://github.com/owner/name/pull/1",
        "target_repo": "owner/name",
        "run_id": "01956000-0000-7000-0000-000000000001",
        "workflow_kind": "spec_driven",
        "door_class": "two_way",
        "agent_owner": "implementer",
        "prompt_variant": "a",
        "opened_at": now(),
        "merged": False,
        "requested_changes_count": 0,
    }
    base.update(overrides)
    return PRTelemetry(**base)


def test_safe_rate_handles_zero_denominator() -> None:
    assert safe_rate(0, 0) == 0.0
    assert safe_rate(5, 0) == 0.0


def test_safe_rate_normal() -> None:
    assert safe_rate(3, 4) == 0.75


def test_bucket_key_uses_repo_owner_variant() -> None:
    row = telemetry(target_repo="x/y", agent_owner="critic", prompt_variant="b")
    assert bucket_key(row) == ("x/y", "critic", "b")


def test_merge_as_is_excludes_one_way() -> None:
    rows = [
        telemetry(merged=True, requested_changes_count=0),  # clean two-way merge
        telemetry(merged=True, requested_changes_count=1),  # iterated two-way merge
        telemetry(door_class="one_way", merged=True),  # excluded
    ]
    merged, total = merge_as_is(rows)
    assert merged == 1
    assert total == 2


def test_one_way_merge_rate_only_counts_one_way() -> None:
    rows = [
        telemetry(merged=True, requested_changes_count=0),  # two-way; ignored
        telemetry(door_class="one_way", merged=True),
        telemetry(door_class="one_way", merged=False, closed_at=now()),
    ]
    merged, total = one_way_merge_rate(rows)
    assert merged == 1
    assert total == 2


def test_rejection_rate_only_counts_closed() -> None:
    rows = [
        telemetry(),  # still open
        telemetry(closed_at=now(), merged=False),
        telemetry(closed_at=now(), merged=True),
    ]
    rejected, closed = rejection_rate(rows)
    assert rejected == 1
    assert closed == 2


def test_weighted_friction_score_uses_committed_table() -> None:
    score = weighted_friction_score({"nit": 5, "bug": 2, "security": 1})
    # nit=0, bug=3 each, security=5 each
    assert score == (0 * 5) + (3 * 2) + (5 * 1)


def test_weighted_friction_score_empty() -> None:
    assert weighted_friction_score({}) == 0.0


def test_median_time_to_merge_handles_no_merged_rows() -> None:
    assert median_time_to_merge_hours([telemetry()]) is None


def test_median_time_to_merge_odd_count() -> None:
    base = now()
    rows = [
        telemetry(merged=True, opened_at=base, merged_at=base + timedelta(hours=1)),
        telemetry(merged=True, opened_at=base, merged_at=base + timedelta(hours=2)),
        telemetry(merged=True, opened_at=base, merged_at=base + timedelta(hours=4)),
    ]
    assert median_time_to_merge_hours(rows) == 2.0


def test_median_time_to_merge_even_count() -> None:
    base = now()
    rows = [
        telemetry(merged=True, opened_at=base, merged_at=base + timedelta(hours=1)),
        telemetry(merged=True, opened_at=base, merged_at=base + timedelta(hours=3)),
    ]
    assert median_time_to_merge_hours(rows) == 2.0


def test_aggregate_groups_by_bucket() -> None:
    rows = [
        telemetry(agent_owner="implementer", merged=True, requested_changes_count=0),
        telemetry(agent_owner="implementer", merged=True, requested_changes_count=2),
        telemetry(agent_owner="reviewer", merged=False, closed_at=now()),
    ]
    metrics = aggregate(
        rows,
        comments={
            ("owner/name", "implementer", "a"): {"design": 1, "nit": 4},
        },
        window_start=now(),
        window_end=now(),
    )
    assert len(metrics) == 2
    by_owner = {m.agent_owner: m for m in metrics}
    impl = by_owner["implementer"]
    assert impl.merge_as_is_rate == 0.5
    assert impl.weighted_friction_score == 3.0  # design=3 * 1 + nit=0 * 4
    assert impl.comments_by_category == {"design": 1, "nit": 4}
    rev = by_owner["reviewer"]
    assert rev.rejection_rate == 1.0


def test_drift_detected_below_sample_size() -> None:
    assert (
        drift_detected(rolling_score=10.0, baseline_score=5.0, sample_size=5) is False
    )


def test_drift_detected_below_threshold() -> None:
    # 5% increase, doesn't trigger 20% threshold
    assert drift_detected(rolling_score=10.5, baseline_score=10.0, sample_size=20) is False


def test_drift_detected_at_threshold() -> None:
    # 20% increase exactly
    assert drift_detected(rolling_score=12.0, baseline_score=10.0, sample_size=20) is True


def test_drift_detected_zero_baseline_with_friction() -> None:
    # Nothing to compare; any rolling friction is drift
    assert drift_detected(rolling_score=5.0, baseline_score=0.0, sample_size=15) is True


def test_drift_detected_zero_baseline_zero_rolling() -> None:
    assert drift_detected(rolling_score=0.0, baseline_score=0.0, sample_size=15) is False


def test_drift_delta_pct_normal() -> None:
    assert drift_delta_pct(rolling_score=12.0, baseline_score=10.0) == 20.0


def test_drift_delta_pct_zero_baseline() -> None:
    assert drift_delta_pct(rolling_score=5.0, baseline_score=0.0) == 100.0
    assert drift_delta_pct(rolling_score=0.0, baseline_score=0.0) == 0.0
