"""Tests for :mod:`state_router.aws` dispatch primitives.

Regression coverage for issue #82: ``dispatch_to_runtime`` previously
only caught :class:`ReadTimeoutError` and :class:`ClientError`. A
:class:`ConnectTimeoutError` (a :class:`BotoCoreError` subclass) would
crash the Lambda *after* the dispatch marker was already published,
wedging the run because ``decide()`` then saw the marker on retry and
returned :class:`Noop` without re-invoking.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from state_router import aws


@pytest.fixture(autouse=True)
def _aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def mock_runtime(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the cached AgentCore runtime client with a MagicMock."""
    client = MagicMock()
    monkeypatch.setattr(aws, "runtime_client", lambda: client)
    return client


def _call() -> bool:
    return aws.dispatch_to_runtime(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/architect",
        runtime_session_id="run-1-architect",
        runtime_user_id="system:state_router",
        payload={"hello": "world"},
    )


def test_returns_true_on_success(mock_runtime: MagicMock) -> None:
    mock_runtime.invoke_agent_runtime.return_value = {"status": "dispatched"}
    assert _call() is True


def test_returns_false_on_read_timeout(mock_runtime: MagicMock) -> None:
    mock_runtime.invoke_agent_runtime.side_effect = ReadTimeoutError(endpoint_url="x")
    assert _call() is False


def test_returns_false_on_connect_timeout(mock_runtime: MagicMock) -> None:
    """Regression: connect timeouts must not propagate (issue #82)."""
    mock_runtime.invoke_agent_runtime.side_effect = ConnectTimeoutError(endpoint_url="x")
    assert _call() is False


def test_returns_false_on_client_error(mock_runtime: MagicMock) -> None:
    err = {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}
    mock_runtime.invoke_agent_runtime.side_effect = ClientError(
        cast(Any, err), "InvokeAgentRuntime"
    )
    assert _call() is False
