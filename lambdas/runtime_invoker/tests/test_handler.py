"""Unit tests for the runtime_invoker shim Lambda."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError, ReadTimeoutError

from runtime_invoker import handler as h


def ctx() -> LambdaContext:
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="runtime-invoker-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:0:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    _safe_cache_clear(h.runtime_client)
    _safe_cache_clear(h.sfn_client)
    yield
    _safe_cache_clear(h.runtime_client)
    _safe_cache_clear(h.sfn_client)


def _safe_cache_clear(fn: Any) -> None:
    """``cache_clear`` only when ``fn`` still wraps an ``@cache``."""
    clear = getattr(fn, "cache_clear", None)
    if clear is not None:
        clear()


def base_event(**overrides: Any) -> dict[str, Any]:
    payload = {
        "task_token": "tok-" + "x" * 60,
        "agent_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:0:runtime/imp-AAAAAAAAAA",
        "runtime_session_id": "session-" + "y" * 30,
        "agent_payload": {"task_id": "T-001", "spec_slug": "s"},
    }
    payload.update(overrides)
    return payload


def test_read_timeout_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.side_effect = ReadTimeoutError(endpoint_url="...")
    monkeypatch.setattr(h, "runtime_client", lambda: fake)

    out = h.handler(base_event(), ctx())

    assert out == {"ok": True, "dispatched": True}
    args = fake.invoke_agent_runtime.call_args.kwargs
    body = json.loads(args["payload"].decode())
    assert body["task_token"].startswith("tok-")
    assert body["task_id"] == "T-001"


def test_immediate_success_is_treated_as_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"contentType": "application/json"}
    monkeypatch.setattr(h, "runtime_client", lambda: fake)

    out = h.handler(base_event(), ctx())

    assert out == {"ok": True, "dispatched": True}


def test_client_error_calls_send_task_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runtime = MagicMock()
    fake_runtime.invoke_agent_runtime.side_effect = ClientError(
        error_response={"Error": {"Code": "AccessDeniedException", "Message": "no perms"}},
        operation_name="InvokeAgentRuntime",
    )
    sent: list[dict[str, Any]] = []
    fake_sfn = MagicMock()
    fake_sfn.send_task_failure.side_effect = lambda **kw: sent.append(kw)

    monkeypatch.setattr(h, "runtime_client", lambda: fake_runtime)
    monkeypatch.setattr(h, "sfn_client", lambda: fake_sfn)

    out = h.handler(base_event(), ctx())

    assert out == {"ok": True, "dispatched": False, "failed": True}
    assert len(sent) == 1
    assert sent[0]["error"] == "AccessDeniedException"
    assert "no perms" in sent[0]["cause"]


def test_validation_rejects_missing_task_token() -> None:
    bad = base_event()
    del bad["task_token"]
    assert h.handler(bad, ctx()) == {"ok": False, "error": "validation_error"}


def test_validation_rejects_short_session_id() -> None:
    bad = base_event(runtime_session_id="too-short")
    assert h.handler(bad, ctx()) == {"ok": False, "error": "validation_error"}
