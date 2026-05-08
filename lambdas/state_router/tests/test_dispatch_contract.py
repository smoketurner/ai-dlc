"""Tests for the AgentCore Runtime dispatch contract.

The agents now implement the AgentCore async-task pattern: the
entrypoint returns ``{"status": "dispatched", ...}`` in ~100ms, and
the actual work runs on a background thread. So any
:class:`ReadTimeoutError` from ``invoke_agent_runtime`` is now a real
failure (no acknowledgement inside the 10s read timeout) rather than
the historical "agent accepted the work, will emit READY when done"
signal.

This test pins the contract so a regression on the timeout semantic
is caught immediately.
"""

from __future__ import annotations

from unittest.mock import patch

from botocore.exceptions import ClientError, ReadTimeoutError

from state_router.aws import dispatch_to_runtime
from state_router.config import DISPATCH_READ_TIMEOUT_SECONDS


def test_read_timeout_is_failure() -> None:
    """ReadTimeoutError now means the runtime didn't acknowledge — failure.

    Pre-async-pattern, a 2s timeout was the success signal because the
    agent ran longer than the timeout. With the new entrypoint
    contract (returns in ~100ms), a 10s timeout firing means real
    failure: rollback + bump the breaker counter.
    """
    fake_client = patch(
        "state_router.aws.runtime_client",
        return_value=type(
            "C",
            (),
            {
                "invoke_agent_runtime": staticmethod(
                    lambda **_: (_ for _ in ()).throw(
                        ReadTimeoutError(endpoint_url="https://x"),
                    ),
                ),
            },
        )(),
    )
    with fake_client:
        ok = dispatch_to_runtime(
            runtime_arn="arn:test",
            runtime_session_id="sess-1",
            payload={"x": 1},
        )
    assert ok is False


def test_client_error_is_failure() -> None:
    """4xx / 5xx from AgentCore — runtime rejected the request — also failure."""
    err = ClientError(
        error_response={"Error": {"Code": "ValidationException", "Message": "bad"}},
        operation_name="InvokeAgentRuntime",
    )
    fake_client = patch(
        "state_router.aws.runtime_client",
        return_value=type(
            "C",
            (),
            {"invoke_agent_runtime": staticmethod(lambda **_: (_ for _ in ()).throw(err))},
        )(),
    )
    with fake_client:
        ok = dispatch_to_runtime(
            runtime_arn="arn:test",
            runtime_session_id="sess-1",
            payload={"x": 1},
        )
    assert ok is False


def test_clean_response_is_success() -> None:
    """A normal dispatch returns ``True`` and consumes no rollback path."""
    fake_client = patch(
        "state_router.aws.runtime_client",
        return_value=type(
            "C",
            (),
            {"invoke_agent_runtime": staticmethod(lambda **_: {"statusCode": 200})},
        )(),
    )
    with fake_client:
        ok = dispatch_to_runtime(
            runtime_arn="arn:test",
            runtime_session_id="sess-1",
            payload={"x": 1},
        )
    assert ok is True


def test_read_timeout_is_set_for_async_pattern() -> None:
    """Sanity-check the timeout has actually been bumped past the old 2s.

    Below ~5s, anything other than the cleanest dispatch would still
    look like a timeout. The async-task entrypoint replies in ~100ms;
    10s gives ample headroom for AgentCore frontend latency without
    wedging on legitimately slow acks.
    """
    assert DISPATCH_READ_TIMEOUT_SECONDS >= 5.0
