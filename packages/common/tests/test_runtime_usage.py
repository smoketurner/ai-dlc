"""Tests for the ``usage_from_strands`` helper.

Stubs Strands' ``Agent.event_loop_metrics`` because we don't want to
construct a real Strands agent in this package's tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from common.runtime import usage_from_strands


def make_agent_stub(
    *,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
) -> Any:
    """Build a minimal object that quacks like Strands' Agent."""
    metrics = SimpleNamespace(
        accumulated_usage={"inputTokens": input_tokens, "outputTokens": output_tokens},
        accumulated_metrics={"latencyMs": latency_ms},
    )
    return SimpleNamespace(event_loop_metrics=metrics)


def test_usage_from_strands_extracts_all_fields() -> None:
    agent = make_agent_stub(input_tokens=4_000, output_tokens=1_500, latency_ms=30_000)

    usage = usage_from_strands(agent, model_id="us.anthropic.claude-sonnet-4-6")

    assert usage["token_in"] == 4_000
    assert usage["token_out"] == 1_500
    assert usage["duration_ms"] == 30_000
    # Sonnet 4.6: $3 in + $15 out per million.
    expected = 4_000 / 1_000_000 * 3.0 + 1_500 / 1_000_000 * 15.0
    assert usage["cost_usd"] == pytest.approx(expected)


def test_usage_from_strands_missing_metrics_returns_zeros() -> None:
    """A bare agent without event_loop_metrics yields all zeros — safe default."""
    agent = SimpleNamespace()

    usage = usage_from_strands(agent, model_id="us.anthropic.claude-opus-4-6-v1")

    assert usage == {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 0}


def test_usage_from_strands_unknown_model_zero_cost() -> None:
    """Unknown model id leaves tokens but zeros cost (pricing-table fallback)."""
    agent = make_agent_stub(input_tokens=1_000, output_tokens=500, latency_ms=5_000)

    usage = usage_from_strands(agent, model_id="anthropic.unknown-model")

    assert usage["token_in"] == 1_000
    assert usage["token_out"] == 500
    assert usage["cost_usd"] == 0.0
