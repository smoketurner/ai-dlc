"""Tests for ``common.github_mentions``."""

from __future__ import annotations

import pytest

from common.github_mentions import (
    bot_mention_re,
    has_bot_mention,
    strip_bot_mention,
)

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


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Bare bot mention reduces to None — no guidance to forward.
        ("@ai-dlc[bot]", None),
        ("  @ai-dlc[bot]  ", None),
        # Bot mention + free-text leaves the free-text behind.
        ("@ai-dlc[bot] please add Z", "please add Z"),
        ("  @ai-dlc[bot]\nplease add Z", "please add Z"),
        # Plain text with no prefix passes through.
        ("just plain guidance", "just plain guidance"),
        # Empty / None inputs return None.
        ("", None),
        (None, None),
        # Mid-body mentions are NOT stripped (only leading).
        ("please ask @ai-dlc[bot] for X", "please ask @ai-dlc[bot] for X"),
    ],
)
def test_strip_bot_mention(body: str | None, expected: str | None) -> None:
    assert strip_bot_mention(body, BOT) == expected


def test_strip_bot_mention_passes_through_when_bot_login_unset() -> None:
    """Without a bot login, the @-prefix passes through verbatim."""
    assert strip_bot_mention("@ai-dlc[bot] please do X", "") == "@ai-dlc[bot] please do X"
    assert strip_bot_mention("plain text", "") == "plain text"
