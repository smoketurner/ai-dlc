"""Tests for ``common.github_mentions``."""

from __future__ import annotations

import pytest

from common.github_mentions import bot_mention_re, has_bot_mention

BOT = "ai-dlc[bot]"


@pytest.mark.parametrize(
    "body",
    [
        "@ai-dlc[bot] please look at this",
        "Hey @ai-dlc[bot], can you fix the lint?",
        "Multiple lines\n@ai-dlc[bot] go fix it\nthanks",
        "@AI-DLC[BOT] should match too (case-insensitive)",
        "Trailing punctuation @ai-dlc[bot]: fix",
        "(parenthesised: @ai-dlc[bot])",
    ],
)
def test_matches_real_mentions(body: str) -> None:
    assert has_bot_mention(body, BOT) is True


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no mention here",
        "email-style noreply@ai-dlc[bot].example.com",
        "/repos/ai-dlc[bot]/something/path",
        "@ai-dlc[bot]suffix-hyphenated should not match (no word boundary at end)",
        "@ai-dlc[bot]_underscored should not match",
        "@ai-dlc[botany] should not match — different login",
    ],
)
def test_rejects_non_mentions(body: str) -> None:
    assert has_bot_mention(body, BOT) is False


def test_none_body_returns_false() -> None:
    assert has_bot_mention(None, BOT) is False


def test_empty_bot_login_disables_matching() -> None:
    assert has_bot_mention("@ai-dlc[bot]", "") is False


def test_special_chars_in_bot_login_escaped() -> None:
    # Brackets in "[bot]" must be escaped — bot_mention_re uses re.escape.
    pattern = bot_mention_re("ai-dlc[bot]")
    assert pattern.search("@ai-dlc[bot]") is not None
    # If brackets weren't escaped, this would match too because [bot] would
    # be a character class meaning "any of b/o/t".
    assert pattern.search("@ai-dlcb") is None
