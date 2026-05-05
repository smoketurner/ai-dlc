"""Shared fixtures for e2e smoke tests."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest


def _make_context(function_name: str = "e2e-smoke") -> Any:
    # SimpleNamespace satisfies the LambdaContext duck-type expected by powertools.
    return SimpleNamespace(
        function_name=function_name,
        memory_limit_in_mb=128,
        invoked_function_arn=f"arn:aws:lambda:us-east-1:000000000000:function:{function_name}",
        aws_request_id="e2e-rid-1",
    )


@pytest.fixture
def invoke() -> Callable[
    [Callable[[dict[str, Any], Any], dict[str, Any]], dict[str, Any]], dict[str, Any]
]:
    """Return a callable that invokes a Lambda handler with a canned context.

    Usage::

        def test_something(invoke):
            result = invoke(my_handler, {"key": "value"})
            assert result["statusCode"] == 200
    """
    ctx = _make_context()

    def call(
        handler: Callable[[dict[str, Any], Any], dict[str, Any]],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        return handler(event, ctx)

    return call  # type: ignore[return-value]
