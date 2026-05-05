"""Tests for ``common.eval`` — production efficiency feedback contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from common.eval import (
    COMMENT_WEIGHT,
    ClassifiedComment,
    CommentCategory,
    DriftSignal,
    EfficiencyMetrics,
    PRTelemetry,
)


def now() -> datetime:
    return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def test_classified_comment_validates() -> None:
    comment = ClassifiedComment(
        pr_url="https://github.com/x/y/pull/1",
        comment_id=42,
        author="alice",
        is_bot=False,
        category="design",
        quoted="This loop reads the whole table into memory.",
        classified_at=now(),
        classifier_model_id="us.anthropic.claude-haiku-4-5-v1",
    )
    assert comment.category == "design"


def test_classified_comment_rejects_invalid_category() -> None:
    with pytest.raises(ValidationError):
        ClassifiedComment.model_validate(
            {
                "pr_url": "x",
                "comment_id": 1,
                "author": "alice",
                "is_bot": False,
                "category": "blocker",  # not in CommentCategory
                "quoted": "x",
                "classified_at": now(),
                "classifier_model_id": "x",
            },
        )


def test_pr_telemetry_validates_minimal() -> None:
    telemetry = PRTelemetry(
        pr_url="https://github.com/x/y/pull/1",
        target_repo="x/y",
        run_id="r1",
        workflow_kind="spec_driven",
        door_class="two_way",
        agent_owner="implementer",
        opened_at=now(),
    )
    assert telemetry.opened_as_draft is False
    assert telemetry.merged is False
    assert telemetry.prompt_variant == "a"


def test_pr_telemetry_target_repo_pattern_enforced() -> None:
    with pytest.raises(ValidationError):
        PRTelemetry(
            pr_url="x",
            target_repo="not-a-repo-slug",
            run_id="r1",
            workflow_kind="spec_driven",
            door_class="two_way",
            agent_owner="implementer",
            opened_at=now(),
        )


def test_pr_telemetry_one_way_draft_lifecycle() -> None:
    telemetry = PRTelemetry(
        pr_url="https://github.com/x/y/pull/1",
        target_repo="x/y",
        run_id="r1",
        spec_slug="add-healthz",
        task_id="T-001",
        workflow_kind="spec_driven",
        door_class="one_way",
        agent_owner="implementer",
        opened_at=now(),
        opened_as_draft=True,
        marked_ready_at=now(),
        marked_ready_by="alice",
        merged_at=now(),
        merged=True,
        review_count=1,
    )
    assert telemetry.opened_as_draft
    assert telemetry.marked_ready_by == "alice"
    assert telemetry.merged


def test_pr_telemetry_negative_counts_rejected() -> None:
    with pytest.raises(ValidationError):
        PRTelemetry(
            pr_url="x",
            target_repo="x/y",
            run_id="r1",
            workflow_kind="spec_driven",
            door_class="two_way",
            agent_owner="implementer",
            opened_at=now(),
            requested_changes_count=-1,
        )


def test_efficiency_metrics_rates_bounded_zero_to_one() -> None:
    with pytest.raises(ValidationError):
        EfficiencyMetrics(
            target_repo="x/y",
            agent_owner="implementer",
            prompt_variant="a",
            window_start=now(),
            window_end=now(),
            pr_count=10,
            merge_as_is_rate=1.5,  # invalid — must be ≤ 1.0
            one_way_merge_rate=0.5,
            weighted_friction_score=0.0,
            rejection_rate=0.0,
        )


def test_efficiency_metrics_validates() -> None:
    metrics = EfficiencyMetrics(
        target_repo="x/y",
        agent_owner="reviewer",
        prompt_variant="a",
        window_start=now(),
        window_end=now(),
        pr_count=42,
        merge_as_is_rate=0.71,
        one_way_merge_rate=1.0,
        weighted_friction_score=23.5,
        median_time_to_merge_hours=18.4,
        rejection_rate=0.05,
        comments_by_category={"nit": 12, "design": 5, "missing_test": 3},
    )
    assert metrics.pr_count == 42
    assert metrics.comments_by_category["design"] == 5


def test_drift_signal_requires_minimum_sample_size() -> None:
    with pytest.raises(ValidationError):
        DriftSignal(
            target_repo="x/y",
            agent_owner="reviewer",
            prompt_variant="a",
            detected_at=now(),
            rolling_window_score=10.0,
            baseline_score=5.0,
            delta_pct=100.0,
            sample_size=5,  # below the C4 floor of 10
            dominant_category="design",
        )


def test_drift_signal_validates() -> None:
    signal = DriftSignal(
        target_repo="x/y",
        agent_owner="architect",
        prompt_variant="a",
        detected_at=now(),
        rolling_window_score=12.5,
        baseline_score=8.0,
        delta_pct=56.25,
        sample_size=15,
        dominant_category="scope",
    )
    assert signal.dominant_category == "scope"
    assert signal.sample_size == 15


def test_comment_weights_match_committed_table() -> None:
    """Commitment C1: NIT=0, BUG=3, DESIGN=3, MISSING_TEST=2, SECURITY=5, SCOPE=4."""
    assert COMMENT_WEIGHT["nit"] == 0
    assert COMMENT_WEIGHT["bug"] == 3
    assert COMMENT_WEIGHT["design"] == 3
    assert COMMENT_WEIGHT["missing_test"] == 2
    assert COMMENT_WEIGHT["security"] == 5
    assert COMMENT_WEIGHT["scope"] == 4


def test_comment_weight_table_covers_every_category() -> None:
    categories = set(get_args(CommentCategory))
    assert set(COMMENT_WEIGHT.keys()) == categories
