"""Tests for the AgentCore runtime identity helpers."""

from __future__ import annotations

import pytest

from common.identity import runtime_user_id


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
