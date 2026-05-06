"""Tests for the Bedrock model pricing table."""

from __future__ import annotations

import pytest

from common.pricing import BEDROCK_PRICING, calculate_cost


def test_known_models_present() -> None:
    """The current production model ids should all be priced."""
    expected = {
        "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    missing = expected - set(BEDROCK_PRICING)
    assert not missing


def test_calculate_cost_uses_per_million_rates() -> None:
    cost = calculate_cost(
        "us.anthropic.claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # Sonnet 4.6: $3 in + $15 out per million.
    assert cost == pytest.approx(18.0)


def test_calculate_cost_zero_tokens() -> None:
    assert calculate_cost(
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        input_tokens=0,
        output_tokens=0,
    ) == pytest.approx(0.0)


def test_calculate_cost_unknown_model_returns_zero() -> None:
    """Surface pricing gaps as zero cost rather than tearing down a successful run."""
    assert calculate_cost("anthropic.unknown-model", input_tokens=1000, output_tokens=500) == 0.0


def test_calculate_cost_partial_tokens() -> None:
    cost = calculate_cost(
        "us.anthropic.claude-opus-4-6-v1",
        input_tokens=4_000,
        output_tokens=1_500,
    )
    # Opus 4.6: $15 in + $75 out per million.
    expected = 4_000 / 1_000_000 * 15.0 + 1_500 / 1_000_000 * 75.0
    assert cost == pytest.approx(expected)
