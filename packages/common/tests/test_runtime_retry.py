"""Tests for ``common.runtime.default_retry_strategy``.

Verifies which kwargs the helper passes to Strands' ``ModelRetryStrategy``
constructor for each model tier. The strategy stores its config in
private attributes, so we patch the constructor and inspect the call
rather than reach into Strands internals.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from common.runtime import default_retry_strategy


def configured_kwargs(model_id: str) -> dict[str, Any]:
    with patch("strands.ModelRetryStrategy") as mocked:
        default_retry_strategy(model_id)
    assert mocked.call_count == 1
    return dict(mocked.call_args.kwargs)


def test_haiku_gets_tighter_backoff() -> None:
    assert configured_kwargs("us.anthropic.claude-haiku-4-5-20251001-v1:0") == {
        "max_attempts": 4,
        "initial_delay": 2,
        "max_delay": 30,
    }


def test_opus_gets_default_strands_policy() -> None:
    assert configured_kwargs("us.anthropic.claude-opus-4-6-v1") == {
        "max_attempts": 6,
        "initial_delay": 4,
        "max_delay": 128,
    }


def test_sonnet_gets_default_strands_policy() -> None:
    assert configured_kwargs("us.anthropic.claude-sonnet-4-6") == {
        "max_attempts": 6,
        "initial_delay": 4,
        "max_delay": 128,
    }


def test_haiku_match_is_case_insensitive() -> None:
    assert configured_kwargs("US.ANTHROPIC.CLAUDE-HAIKU-4-5")["max_attempts"] == 4
