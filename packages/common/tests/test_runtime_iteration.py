"""Tests for the iteration-mode additions to ``common.runtime``."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from common.runtime import (
    CiFailureFeedback,
    FeedbackItem,
    ImplementerInput,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)


def base_input(**overrides: object) -> ImplementerInput:
    """Build a minimum-valid ImplementerInput, allowing per-test overrides."""
    fields: dict[str, object] = {
        "project_slug": "demo",
        "spec_slug": "add-healthz",
        "spec_s3_prefix": "specs/add-healthz/",
        "task_id": "T-001",
        "run_id": "r1",
        "correlation_id": "c1",
        "target_repo": "owner/repo",
    }
    fields.update(overrides)
    return ImplementerInput.model_validate(fields)


def test_implementer_input_defaults_no_iteration() -> None:
    payload = base_input()
    assert payload.iteration_count == 0
    assert payload.iteration_feedback is None
    assert payload.task_token is None


def test_implementer_input_iteration_round_trip() -> None:
    feedback: list[FeedbackItem] = [
        CiFailureFeedback(
            workflow_name="CI / test",
            conclusion="failure",
            head_sha="abcdef0123456",
            html_url="https://github.com/x/y/actions/runs/1",
        ),
        ReviewCommentMentionFeedback(
            path="src/handler.py",
            line=42,
            commit_id="abcdef0123456",
            comment_id=99,
            body="@ai-dlc[bot] this null-check is wrong",
            commenter="alice",
        ),
    ]
    payload = base_input(iteration_count=1, iteration_feedback=feedback)
    parsed = ImplementerInput.model_validate_json(payload.model_dump_json())
    assert parsed.iteration_count == 1
    assert parsed.iteration_feedback is not None
    assert len(parsed.iteration_feedback) == 2
    assert parsed.iteration_feedback[0].kind == "ci_failure"
    assert parsed.iteration_feedback[1].kind == "review_comment_mention"


def test_implementer_input_rejects_iteration_over_cap() -> None:
    with pytest.raises(ValidationError):
        base_input(iteration_count=17)


def test_feedback_discriminator_picks_right_class() -> None:
    adapter = TypeAdapter(FeedbackItem)
    parsed = adapter.validate_python(
        {
            "kind": "issue_comment_mention",
            "comment_id": 12,
            "body": "@ai-dlc[bot] take another look",
            "commenter": "bob",
        },
    )
    assert isinstance(parsed, IssueCommentMentionFeedback)
    assert parsed.commenter == "bob"


def test_feedback_discriminator_rejects_unknown_kind() -> None:
    adapter = TypeAdapter(FeedbackItem)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "made_up", "body": "x", "commenter": "x"})


def test_review_changes_requested_minimal_fields() -> None:
    fb = ReviewChangesRequestedFeedback(reviewer="alice", review_id=1)
    assert fb.body == ""
    assert fb.kind == "review_changes_requested"


def test_ci_failure_rejects_unknown_conclusion() -> None:
    with pytest.raises(ValidationError):
        CiFailureFeedback.model_validate(
            {
                "kind": "ci_failure",
                "workflow_name": "x",
                "conclusion": "success",
                "head_sha": "abcdef0",
                "html_url": "https://github.com/x/y/actions/runs/1",
            },
        )
