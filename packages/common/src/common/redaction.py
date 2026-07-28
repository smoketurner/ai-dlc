"""Strip short-lived credentials out of text bound for logs, events, or the UI.

Git operations authenticate by embedding a GitHub App installation token in
the clone URL (``https://x-access-token:<token>@github.com/...``). Any failure
of those commands produces an exception whose string form carries the full
argv — including the token. Those strings reach CloudWatch via
``logger.exception`` and the runs table via ``RunFailed.error_message``, which
the dashboard renders to every authenticated user.

Redact at the point text is produced, not at the point it is displayed: the
event envelope is persisted verbatim by the projector, so anything that leaks
into a payload is durable while the token is only valid for an hour.
"""

from __future__ import annotations

import re

TOKEN_URL_PATTERN = re.compile(r"(x-access-token:)[^@\s/]+@")
"""Userinfo credential in a git remote URL."""

GH_TOKEN_PATTERN = re.compile(r"\b(gh[posru]_|github_pat_)[A-Za-z0-9_]+")
"""Bare GitHub token by its documented prefix, wherever it appears."""

ARCHIVE_TOKEN_PATTERN = re.compile(r"([?&]token=)[^&\s]+")
"""``?token=`` query parameter on a codeload archive URL."""

REDACTED = "<redacted>"


def redact_secrets(text: str) -> str:
    """Replace every known credential shape in ``text`` with a placeholder.

    Safe to apply more than once — the placeholder matches none of the
    patterns.

    Args:
        text: Arbitrary text that may embed a credential.

    Returns:
        The same text with credentials replaced by ``<redacted>``.
    """
    text = TOKEN_URL_PATTERN.sub(rf"\1{REDACTED}@", text)
    text = ARCHIVE_TOKEN_PATTERN.sub(rf"\1{REDACTED}", text)
    return GH_TOKEN_PATTERN.sub(REDACTED, text)
