"""Tests for ``state_router.extract`` revision-feedback mapping.

Each ``IMPL.ITERATION_REQUESTED`` source maps to one discriminated
``FeedbackItem`` variant, and each variant requires the id from its own
GitHub namespace. Getting this wrong sends a review id into a
comment-id field, which 404s downstream.
"""

from __future__ import annotations

from typing import Any

import pytest

from common.runtime import ImplementerInput, ReviewCommentMentionFeedback
from state_router.extract import issue_payload, revision_feedback


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


def test_review_comment_mention_threads_path_line_commit_id() -> None:
    """Webhook-provided file/line/commit context reaches the feedback item."""
    feedback = revision_feedback(
        [
            pr_opened(),
            iteration(
                source="review_comment_mention",
                comment_id=8,
                path="src/handler.py",
                line=42,
                commit_id="abcdef0",
            ),
        ],
    )

    assert feedback[0] == {
        "kind": "review_comment_mention",
        "path": "src/handler.py",
        "line": 42,
        "commit_id": "abcdef0",
        "comment_id": 8,
        "body": "@aidlc-bot take another look",
        "commenter": "alice",
    }


def test_review_comment_mention_validates_against_implementer_input_with_real_context() -> None:
    """The extracted item with real values satisfies the discriminated union."""
    events = [
        pr_opened(),
        iteration(
            source="review_comment_mention",
            comment_id=8,
            path="src/handler.py",
            line=42,
            commit_id="abcdef0123456",
        ),
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
    item = payload.revision_feedback
    assert item is not None
    feedback_item = item[0]
    assert isinstance(feedback_item, ReviewCommentMentionFeedback)
    assert feedback_item.path == "src/handler.py"
    assert feedback_item.line == 42
    assert feedback_item.commit_id == "abcdef0123456"


def test_review_comment_mention_falls_back_when_context_missing() -> None:
    """Legacy events without path/commit_id still produce a schema-valid item."""
    feedback = revision_feedback(
        [pr_opened(), iteration(source="review_comment_mention", comment_id=8)],
    )

    assert feedback[0]["path"] == "(unknown)"
    assert feedback[0]["line"] is None
    assert feedback[0]["commit_id"] == "0" * 7


# --- issue_payload -----------------------------------------------------------


def _request_received(
    *,
    issue_url: str = "https://github.com/x/y/issues/1",
    issue_number: int = 1,
    issue_title: str = "Bug: Login fails",
    issue_body: str = "## Steps to reproduce\n1. Open app\n2. Click login",
    issue_labels: list[str] | None = None,
    event_id: str = "evt-1",
) -> Env:
    return Env(
        event_type="REQUEST.RECEIVED",
        event_id=event_id,
        payload={
            "source_issue_url": issue_url,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "issue_body": issue_body,
            "issue_labels": ["bug"] if issue_labels is None else issue_labels,
        },
    )


def _issue_triaged(
    *,
    issue_url: str = "https://github.com/x/y/issues/1",
    issue_number: int = 1,
    action: str = "proceed",
    rationale: str = "Clear bug report with repro steps",
    event_id: str = "evt-2",
) -> Env:
    return Env(
        event_type="ISSUE.TRIAGED",
        event_id=event_id,
        payload={
            "issue_url": issue_url,
            "issue_number": issue_number,
            "action": action,
            "rationale": rationale,
        },
    )


def test_issue_payload_after_triaged_falls_back_to_request_received() -> None:
    """After ISSUE.TRIAGED, issue_payload should get body/title from REQUEST.RECEIVED."""
    events = [
        _request_received(
            issue_title="Bug: Login fails",
            issue_body="## Steps to reproduce\n1. Open app\n2. Click login",
            issue_labels=["bug"],
        ),
        _issue_triaged(),
    ]
    result = issue_payload(events)
    assert result["issue_title"] == "Bug: Login fails", "Title should come from REQUEST.RECEIVED"
    assert result["issue_body"] == "## Steps to reproduce\n1. Open app\n2. Click login", (
        "Body should come from REQUEST.RECEIVED"
    )
    assert result["issue_labels"] == ["bug"], "Labels should come from REQUEST.RECEIVED"


def test_issue_payload_after_triaged_uses_triaged_url_and_number() -> None:
    """TRIAGED remains the canonical source for url/number after triage."""
    events = [
        _request_received(
            issue_url="https://github.com/x/y/issues/1",
            issue_number=1,
        ),
        _issue_triaged(
            issue_url="https://github.com/x/y/issues/1",
            issue_number=1,
        ),
    ]
    result = issue_payload(events)
    assert result["issue_url"] == "https://github.com/x/y/issues/1"
    assert result["issue_number"] == 1


def test_issue_payload_without_triaged_reads_from_request_received() -> None:
    """Pre-triage path still sources everything from REQUEST.RECEIVED."""
    events = [_request_received()]
    result = issue_payload(events)
    assert result == {
        "issue_url": "https://github.com/x/y/issues/1",
        "issue_number": 1,
        "issue_title": "Bug: Login fails",
        "issue_body": "## Steps to reproduce\n1. Open app\n2. Click login",
        "issue_labels": ["bug"],
    }


def test_issue_payload_returns_empty_when_no_relevant_events() -> None:
    """No REQUEST.RECEIVED and no ISSUE.TRIAGED → empty dict (no crash)."""
    events: list[Env] = []
    assert issue_payload(events) == {}


def test_issue_payload_after_triaged_preserves_empty_body_and_labels() -> None:
    """An issue with no body/labels is passed through as empty, not dropped."""
    events = [
        _request_received(
            issue_title="Empty body issue",
            issue_body="",
            issue_labels=[],
        ),
        _issue_triaged(),
    ]
    result = issue_payload(events)
    assert result["issue_title"] == "Empty body issue"
    assert result["issue_body"] == ""
    assert result["issue_labels"] == []


def test_issue_payload_after_triaged_preserves_none_labels_as_empty() -> None:
    """A missing issue_labels field degrades to [] rather than raising."""
    events = [
        Env(
            event_type="REQUEST.RECEIVED",
            event_id="evt-1",
            payload={
                "source_issue_url": "https://github.com/x/y/issues/1",
                "issue_number": 1,
                "issue_title": "No labels here",
                "issue_body": "body",
            },
        ),
        _issue_triaged(),
    ]
    result = issue_payload(events)
    assert result["issue_labels"] == []


def test_issue_payload_returns_new_list_instance_each_call() -> None:
    """Mutating the returned labels list must not poison subsequent calls."""
    events = [_request_received(issue_labels=["bug"]), _issue_triaged()]
    first = issue_payload(events)
    first["issue_labels"].append("tampered")
    second = issue_payload(events)
    assert second["issue_labels"] == ["bug"]
