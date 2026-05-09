"""Tests for retrospector_dispatcher.handler — event classification + invocation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext

from retrospector_dispatcher import handler as dispatcher
from retrospector_dispatcher.handler import (
    build_retrospector_input,
    derive_target_repo,
    handler,
)


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_RETROSPECTOR_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/r-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    dispatcher.agentcore_client.cache_clear()


def ctx() -> LambdaContext:
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="retrospector-dispatcher-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


def envelope(event_type: str, **payload_overrides: Any) -> dict[str, Any]:
    """Build a platform event envelope wrapped in EventBridge shape."""
    inner = {
        "schema_version": "1.0",
        "event_id": "01J0000000000000000000000A",
        "type": event_type,
        "timestamp": "2026-05-09T12:00:00Z",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
        "correlation_id": "019e0e69-198d-7263-8bfc-7eb9e8ae05df",
        "actor_id": "webhook",
        "payload": {
            "project_slug": "ai-dlc",
            "spec_slug": "lint-gate",
            "task_id": "T-001",
            "pr_url": "https://github.com/smoketurner/ai-dlc/pull/42",
            "reviewer": "alice",
            **payload_overrides,
        },
    }
    return {
        "version": "0",
        "id": "11111111-2222-3333-4444-555555555555",
        "detail-type": event_type,
        "source": "ai-dlc.system",
        "account": "000000000000",
        "time": "2026-05-09T12:00:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail": inner,
    }


def test_derive_target_repo_extracts_owner_name_from_pr_url() -> None:
    assert (
        derive_target_repo(
            pr_url="https://github.com/smoketurner/ai-dlc/pull/42",
            issue_url="",
        )
        == "smoketurner/ai-dlc"
    )


def test_derive_target_repo_falls_back_to_issue_url() -> None:
    assert (
        derive_target_repo(
            pr_url="",
            issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        )
        == "smoketurner/ai-dlc"
    )


def test_derive_target_repo_returns_empty_for_non_github_url() -> None:
    assert derive_target_repo(pr_url="https://example.com/x/y", issue_url="") == ""


def test_handler_invokes_runtime_for_task_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"statusCode": 200}
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("TASK.APPROVED"), ctx())
    assert out == {
        "ok": True,
        "dispatched": "TASK.APPROVED",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
    }
    assert fake.invoke_agent_runtime.call_count == 1
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["event_type"] == "TASK.APPROVED"
    assert body["target_repo"] == "smoketurner/ai-dlc"
    assert body["spec_slug"] == "lint-gate"
    assert body["task_id"] == "T-001"
    assert body["pr_url"] == "https://github.com/smoketurner/ai-dlc/pull/42"


def test_handler_invokes_runtime_for_run_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"statusCode": 200}
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    event = envelope(
        "RUN.CANCEL_REQUESTED",
        pr_url=None,
        spec_slug=None,
        task_id=None,
        source_issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        reviewer=None,
        requestor="alice",
        source="issue_closed",
        reason="issue closed by alice",
    )
    out = handler(event, ctx())
    assert out["ok"] is True
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["event_type"] == "RUN.CANCEL_REQUESTED"
    assert body["issue_url"] == "https://github.com/smoketurner/ai-dlc/issues/9"
    assert body["pr_url"] == ""
    assert body["reviewer"] == "alice"  # falls through from `requestor`
    assert body["reason"] == "issue closed by alice"


def test_handler_ignores_non_terminal_events(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("SPEC.READY"), ctx())
    assert out == {"ok": True, "ignored": "SPEC.READY"}
    fake.invoke_agent_runtime.assert_not_called()


def test_handler_ignores_event_missing_project_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("TASK.APPROVED", project_slug=""), ctx())
    assert out == {"ok": True, "ignored": "missing_fields"}
    fake.invoke_agent_runtime.assert_not_called()


def test_handler_ignores_event_with_neither_pr_nor_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("TASK.APPROVED", pr_url=""), ctx())
    assert out == {"ok": True, "ignored": "missing_fields"}
    fake.invoke_agent_runtime.assert_not_called()


def test_handler_returns_validation_error_for_malformed_envelope() -> None:
    out = handler({"source": "ai-dlc.test"}, ctx())
    assert out == {"ok": False, "error": "validation_error"}


def test_build_retrospector_input_extracts_target_repo_from_pr_url() -> None:
    """The agent input carries owner/name parsed out of the PR URL."""
    fake = SimpleNamespace(
        type="TASK.APPROVED",
        run_id="r-1",
        correlation_id="c-1",
        payload={
            "project_slug": "ai-dlc",
            "pr_url": "https://github.com/o/r/pull/1",
            "spec_slug": "demo",
            "task_id": "T-1",
            "reviewer": "alice",
        },
    )
    payload = build_retrospector_input(cast("Any", fake))
    assert payload is not None
    assert payload["target_repo"] == "o/r"
