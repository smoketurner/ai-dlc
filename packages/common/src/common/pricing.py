"""Per-model price table for Anthropic models on Bedrock.

Each agent's framework reports input + output token counts but not cost
in dollars (Strands does not; the Claude Agent SDK does report cost
directly so the implementer skips this table). The values are the
public Anthropic on-Bedrock pricing as of 2026-05; update when prices
change. The table is small and stable enough to live in code.

Cost is computed as::

    cost = (input_tokens / 1_000_000) * input_price
         + (output_tokens / 1_000_000) * output_price
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-million-token price in USD for one model."""

    input_per_million: float
    output_per_million: float


# Inference profile / Bedrock model IDs the platform uses today, plus
# the matching base ARN forms (us.anthropic.claude-* vs anthropic.claude-*)
# that show up across regions.
BEDROCK_PRICING: dict[str, ModelPrice] = {
    # Claude Opus 4.x — Architect, Critic, Proposer.
    "us.anthropic.claude-opus-4-6-v1": ModelPrice(15.0, 75.0),
    "us.anthropic.claude-opus-4-7-v1": ModelPrice(15.0, 75.0),
    "anthropic.claude-opus-4-6-v1": ModelPrice(15.0, 75.0),
    # Claude Sonnet 4.x — Implementer, Reviewer.
    "us.anthropic.claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "anthropic.claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    # Claude Haiku 4.x — Tester, Triage, comment_classifier, telemetry.
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": ModelPrice(1.0, 5.0),
    "anthropic.claude-haiku-4-5-20251001-v1:0": ModelPrice(1.0, 5.0),
}

DEFAULT_PRICE = ModelPrice(0.0, 0.0)


def calculate_cost(model_id: str, *, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for one model invocation.

    Returns ``0.0`` for unknown models — surfacing pricing gaps as a zero
    cost is preferable to raising and tearing down a successful run.
    Add the model id to :data:`BEDROCK_PRICING` when this happens.
    """
    price = BEDROCK_PRICING.get(model_id, DEFAULT_PRICE)
    return (input_tokens / 1_000_000) * price.input_per_million + (
        output_tokens / 1_000_000
    ) * price.output_per_million
