"""Tests for ``common.runtime.invoke_with_fallback``.

Covers the four observable behaviours: primary succeeds, primary
throttles and fallback succeeds, throttle without a configured fallback
re-raises, and throttle when the fallback id matches the primary
re-raises (no infinite retry).
"""

from __future__ import annotations

from typing import Any

import pytest
from strands.types.exceptions import ModelThrottledException

from common.runtime import invoke_with_fallback

PRIMARY = "us.anthropic.claude-opus-4-6-v1"
FALLBACK = "us.anthropic.claude-sonnet-4-6"


class FakeAgent:
    """Sentinel object passed through ``build`` → ``run``."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id


def make_builder() -> tuple[list[str], Any]:
    """Return ``(seen_model_ids, build)`` so tests can assert call order."""
    seen: list[str] = []

    def build(model_id: str) -> FakeAgent:
        seen.append(model_id)
        return FakeAgent(model_id)

    return seen, build


def test_primary_success_runs_only_primary() -> None:
    seen, build = make_builder()
    run_calls: list[FakeAgent] = []

    def run(agent: FakeAgent) -> str:
        run_calls.append(agent)
        return "primary-result"

    agent, used, result = invoke_with_fallback(
        primary_model_id=PRIMARY,
        fallback_model_id=FALLBACK,
        build=build,
        run=run,
    )

    assert seen == [PRIMARY]
    assert len(run_calls) == 1
    assert agent.model_id == PRIMARY
    assert used == PRIMARY
    assert result == "primary-result"


def test_primary_throttled_falls_back_to_secondary() -> None:
    seen, build = make_builder()
    invocations: list[str] = []

    def run(agent: FakeAgent) -> str:
        invocations.append(agent.model_id)
        if agent.model_id == PRIMARY:
            raise ModelThrottledException("daily token cap exhausted")
        return "fallback-result"

    agent, used, result = invoke_with_fallback(
        primary_model_id=PRIMARY,
        fallback_model_id=FALLBACK,
        build=build,
        run=run,
    )

    assert seen == [PRIMARY, FALLBACK]
    assert invocations == [PRIMARY, FALLBACK]
    assert agent.model_id == FALLBACK
    assert used == FALLBACK
    assert result == "fallback-result"


@pytest.mark.parametrize("fallback", [None, ""])
def test_throttle_without_fallback_propagates(fallback: str | None) -> None:
    seen, build = make_builder()

    def run(_: FakeAgent) -> str:
        raise ModelThrottledException("nope")

    with pytest.raises(ModelThrottledException, match="nope"):
        invoke_with_fallback(
            primary_model_id=PRIMARY,
            fallback_model_id=fallback,
            build=build,
            run=run,
        )
    assert seen == [PRIMARY]


def test_throttle_with_identical_fallback_propagates() -> None:
    """An ops misconfiguration that points the fallback at the primary
    must not produce an infinite retry — re-raise immediately."""
    seen, build = make_builder()

    def run(_: FakeAgent) -> str:
        raise ModelThrottledException("still throttled")

    with pytest.raises(ModelThrottledException):
        invoke_with_fallback(
            primary_model_id=PRIMARY,
            fallback_model_id=PRIMARY,
            build=build,
            run=run,
        )
    assert seen == [PRIMARY]


def test_fallback_throttle_propagates() -> None:
    """Both models throttling means the run fails for real — surface it."""
    seen, build = make_builder()

    def run(_: FakeAgent) -> str:
        raise ModelThrottledException("both out")

    with pytest.raises(ModelThrottledException, match="both out"):
        invoke_with_fallback(
            primary_model_id=PRIMARY,
            fallback_model_id=FALLBACK,
            build=build,
            run=run,
        )
    assert seen == [PRIMARY, FALLBACK]


def test_non_throttle_exception_does_not_fall_back() -> None:
    """Only ``ModelThrottledException`` triggers fallback; other errors propagate."""
    seen, build = make_builder()

    def run(_: FakeAgent) -> str:
        raise ValueError("bug in run fn")

    with pytest.raises(ValueError, match="bug in run fn"):
        invoke_with_fallback(
            primary_model_id=PRIMARY,
            fallback_model_id=FALLBACK,
            build=build,
            run=run,
        )
    assert seen == [PRIMARY]
