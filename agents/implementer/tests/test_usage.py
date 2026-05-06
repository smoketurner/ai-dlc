"""Tests for ``client.extract_usage`` — pulls usage off ResultMessage."""

from __future__ import annotations

from claude_agent_sdk import ResultMessage

from implementer.client import extract_usage


def make_result(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: float | None,
    duration_ms: int = 1_000,
) -> ResultMessage:
    """Build a real ResultMessage with controllable usage fields."""
    usage: dict[str, object] | None = None
    if input_tokens is not None or output_tokens is not None:
        usage = {}
        if input_tokens is not None:
            usage["input_tokens"] = input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
    return ResultMessage(
        subtype="success",
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        is_error=False,
        num_turns=3,
        session_id="sess-1",
        total_cost_usd=cost,
        usage=usage,
    )


def test_extract_usage_happy_path() -> None:
    msg = make_result(input_tokens=4_000, output_tokens=1_500, cost=0.18, duration_ms=30_000)

    usage = extract_usage(msg)

    assert usage == {
        "token_in": 4_000,
        "token_out": 1_500,
        "cost_usd": 0.18,
        "duration_ms": 30_000,
    }


def test_extract_usage_missing_usage_dict_zeros() -> None:
    msg = make_result(input_tokens=None, output_tokens=None, cost=0.0)

    usage = extract_usage(msg)

    assert usage == {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 1_000}


def test_extract_usage_none_cost_treated_as_zero() -> None:
    msg = make_result(input_tokens=10, output_tokens=20, cost=None)

    usage = extract_usage(msg)

    assert usage["cost_usd"] == 0.0
    assert usage["token_in"] == 10
