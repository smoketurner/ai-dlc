r"""GitHub @-mention detection for the iteration trigger path.

The webhook receiver routes a PR comment / review-thread comment to the
iteration_reactor only when it @-mentions the configured bot login (e.g.
``@ai-dlc[bot]``). This module provides the regex helper both webhook
parsers reuse so the matching rule stays consistent.

The regex is parameterised on ``bot_login`` (the GitHub username of the
GitHub App's bot user, including the trailing ``[bot]`` suffix) because
GitHub Apps end in literal ``[bot]`` — square brackets are word-boundary
characters so a naive ``\b`` doesn't anchor correctly. We use lookarounds
that exclude word characters and ``-`` / ``/`` (the latter to avoid
matching the bot login as part of a path like ``/repos/foo[bot]/...``).
"""

from __future__ import annotations

import re
from functools import cache


@cache
def bot_mention_re(bot_login: str) -> re.Pattern[str]:
    """Return a cached compiled pattern matching ``@<bot_login>`` in text.

    Case-insensitive. Anchored so ``email@ai-dlc[bot].example`` and
    ``/users/ai-dlc[bot]`` do NOT match — only standalone @-mentions.
    """
    escaped = re.escape(bot_login)
    return re.compile(rf"(?<![\w/])@{escaped}(?![\w-])", re.IGNORECASE)


def has_bot_mention(body: str | None, bot_login: str) -> bool:
    """``True`` if ``body`` contains an @-mention of the bot.

    Returns ``False`` for empty / missing bodies and empty ``bot_login``
    (the unconfigured-bot-login case — webhook handler treats that as
    "iteration disabled").
    """
    if not body or not bot_login:
        return False
    return bot_mention_re(bot_login).search(body) is not None
