"""Tests for the AgentCore runtime identity helpers."""

from __future__ import annotations

import pytest

from common.identity import revision_commenter, runtime_user_id


class TestRuntimeUserId:
    """Precedence + namespacing rules for ``runtime_user_id``."""

    def test_cognito_sub_wins_over_requestor(self) -> None:
        result = runtime_user_id(requestor_sub="abc-123", requestor="jplock")
        assert result == "cognito:abc-123"

    def test_human_github_login(self) -> None:
        result = runtime_user_id(requestor="jplock")
        assert result == "gh:jplock"

    def test_bot_login_keeps_brackets(self) -> None:
        result = runtime_user_id(requestor="ai-dlc-dev[bot]")
        assert result == "gh-app:ai-dlc-dev[bot]"

    def test_blank_requestor_uses_fallback(self) -> None:
        result = runtime_user_id(requestor="   ")
        assert result == "system:unknown"

    def test_empty_inputs_use_fallback(self) -> None:
        result = runtime_user_id(requestor_sub=None, requestor=None)
        assert result == "system:unknown"

    def test_caller_supplied_fallback(self) -> None:
        result = runtime_user_id(requestor=None, fallback="system:retrospector")
        assert result == "system:retrospector"

    def test_strips_surrounding_whitespace(self) -> None:
        result = runtime_user_id(requestor="  jplock  ")
        assert result == "gh:jplock"


class TestRevisionCommenter:
    """Walks the feedback queue from newest to oldest, skipping CI items."""

    def test_returns_latest_commenter(self) -> None:
        feedback: list[dict[str, object]] = [
            {
                "kind": "issue_comment_mention",
                "commenter": "alice",
                "body": "first",
                "comment_id": 1,
            },
            {
                "kind": "review_comment_mention",
                "commenter": "bob",
                "body": "second",
                "comment_id": 2,
                "commit_id": "abc",
                "path": "x",
            },
        ]
        assert revision_commenter(feedback) == "bob"

    def test_returns_reviewer_for_changes_requested(self) -> None:
        feedback: list[dict[str, object]] = [
            {"kind": "review_changes_requested", "reviewer": "carol", "body": "no", "review_id": 1},
        ]
        assert revision_commenter(feedback) == "carol"

    def test_skips_ci_failures(self) -> None:
        feedback: list[dict[str, object]] = [
            {"kind": "issue_comment_mention", "commenter": "dave", "body": "fix", "comment_id": 1},
            {
                "kind": "ci_failure",
                "workflow_name": "ci",
                "conclusion": "failure",
                "head_sha": "deadbeef",
                "html_url": "",
            },
        ]
        # Latest is a ci_failure → skip, fall back to the prior commenter.
        assert revision_commenter(feedback) == "dave"

    def test_empty_queue_returns_none(self) -> None:
        assert revision_commenter([]) is None

    def test_only_ci_failures_returns_none(self) -> None:
        feedback: list[dict[str, object]] = [
            {
                "kind": "ci_failure",
                "workflow_name": "ci",
                "conclusion": "failure",
                "head_sha": "deadbeef",
                "html_url": "",
            },
        ]
        assert revision_commenter(feedback) is None

    def test_unknown_kind_skipped(self) -> None:
        feedback: list[dict[str, object]] = [
            {"kind": "issue_comment_mention", "commenter": "ellie", "body": "x", "comment_id": 1},
            {"kind": "future_variant", "commenter": "ignored"},
        ]
        # Unknown discriminator → caller hasn't been updated to handle it; skip.
        assert revision_commenter(feedback) == "ellie"


@pytest.mark.parametrize(
    ("requestor", "expected"),
    [
        ("github-actions[bot]", "gh-app:github-actions[bot]"),
        ("dependabot[bot]", "gh-app:dependabot[bot]"),
        ("ai-dlc-dev[bot]", "gh-app:ai-dlc-dev[bot]"),
        ("renovate-bot", "gh:renovate-bot"),
    ],
)
def test_bot_detection_by_suffix(requestor: str, expected: str) -> None:
    """Only the ``[bot]`` suffix triggers the ``gh-app:`` namespace."""
    assert runtime_user_id(requestor=requestor) == expected
