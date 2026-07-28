"""Tests for ``state_router.extract`` revision-feedback mapping.

Each ``IMPL.ITERATION_REQUESTED`` source maps to one discriminated
``FeedbackItem`` variant, and each variant requires the id from its own
GitHub namespace. Getting this wrong sends a review id into a
comment-id field, which 404s downstream.
"""

from __future__ import annotations

from typing import Any

import pytest

from common.runtime import ImplementerInput
from state_router.extract import revision_feedback


class Env:
    """Minimal envelope shim matching :class:`EnvelopeLike`."""

    def __init__(self, *, event_type: str, event_id: str, payload: dict[str, Any]) -> None:
        self.type = event_type
        self.event_id = event_id
        self.run_id = "run-1"
        self.correlation_id = "corr-1"
        self.payload = payload


def pr_opened() -> Env:
    return Env(
        event_type="IMPL_PR.OPENED",
        event_id="evt-pr",
        payload={"project_slug": "demo", "pr_url": "https://github.com/o/r/pull/1"},
    )


def iteration(**payload: Any) -> Env:
    return Env(
        event_type="IMPL.ITERATION_REQUESTED",
        event_id="evt-it",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/1",
            "delivery_id": "d-1",
            "feedback_body": "@aidlc-bot take another look",
            "commenter": "alice",
            **payload,
        },
    )


def test_review_mention_maps_to_review_id() -> None:
    feedback = revision_feedback([pr_opened(), iteration(source="review_mention", review_id=99)])

    assert feedback == (
        {
            "kind": "review_mention",
            "reviewer": "alice",
            "body": "@aidlc-bot take another look",
            "review_id": 99,
        },
    )


def test_review_changes_requested_maps_to_review_id() -> None:
    feedback = revision_feedback(
        [pr_opened(), iteration(source="review_changes_requested", review_id=42)],
    )

    assert feedback[0]["kind"] == "review_changes_requested"
    assert feedback[0]["review_id"] == 42


def test_issue_comment_mention_maps_to_comment_id() -> None:
    feedback = revision_feedback(
        [pr_opened(), iteration(source="issue_comment_mention", comment_id=7)],
    )

    assert feedback[0]["kind"] == "issue_comment_mention"
    assert feedback[0]["comment_id"] == 7


@pytest.mark.parametrize(
    ("source", "wrong_id"),
    [
        ("review_mention", {"comment_id": 99}),
        ("review_changes_requested", {"comment_id": 42}),
        ("issue_comment_mention", {"review_id": 7}),
        ("review_comment_mention", {"review_id": 7}),
    ],
)
def test_id_from_the_wrong_namespace_is_dropped(source: str, wrong_id: dict[str, int]) -> None:
    """A missing same-namespace id drops the item rather than mislabelling it."""
    assert revision_feedback([pr_opened(), iteration(source=source, **wrong_id)]) == ()


def test_every_variant_validates_against_the_implementer_input() -> None:
    """The extracted dicts must satisfy the discriminated union on the payload."""
    events = [
        pr_opened(),
        iteration(source="review_mention", review_id=99),
        iteration(source="review_changes_requested", review_id=42),
        iteration(source="issue_comment_mention", comment_id=7),
        iteration(source="review_comment_mention", comment_id=8),
    ]

    payload = ImplementerInput.model_validate(
        {
            "project_slug": "demo",
            "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
            "correlation_id": "019e0e69-198d-7263-8bfc-7eb9e8ae05df",
            "target_repo": "o/r",
            "mode": "revision",
            "revision_number": 1,
            "revision_feedback": list(revision_feedback(events)),
        },
    )

    assert [item.kind for item in payload.revision_feedback or []] == [
        "review_mention",
        "review_changes_requested",
        "issue_comment_mention",
        "review_comment_mention",
    ]


def test_feedback_resets_after_each_completed_revision() -> None:
    events = [
        pr_opened(),
        iteration(source="review_mention", review_id=99),
        Env(event_type="REVISION.READY", event_id="evt-rr", payload={"project_slug": "demo"}),
        iteration(source="issue_comment_mention", comment_id=7),
    ]

    feedback = revision_feedback(events)

    assert len(feedback) == 1
    assert feedback[0]["kind"] == "issue_comment_mention"
