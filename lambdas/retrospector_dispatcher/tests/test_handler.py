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
            "pr_url": "https://github.com/smoketurner/ai-dlc/pull/42",
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


def test_handler_invokes_runtime_for_run_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"statusCode": 200}
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("RUN.COMPLETED"), ctx())
    assert out == {
        "ok": True,
        "dispatched": "RUN.COMPLETED",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
    }
    assert fake.invoke_agent_runtime.call_count == 1
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["event_type"] == "RUN.COMPLETED"
    assert body["target_repo"] == "smoketurner/ai-dlc"
    assert body["pr_url"] == "https://github.com/smoketurner/ai-dlc/pull/42"
    # Dropped fields no longer present.
    assert "spec_slug" not in body
    assert "task_id" not in body
    assert "reviewer" not in body


def test_handler_invokes_runtime_for_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"statusCode": 200}
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(
        envelope("RUN.FAILED", reason="reviewer requested changes 3 times"),
        ctx(),
    )
    assert out["ok"] is True
    assert out["dispatched"] == "RUN.FAILED"
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["event_type"] == "RUN.FAILED"
    assert body["reason"] == "reviewer requested changes 3 times"


def test_handler_invokes_runtime_for_run_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"statusCode": 200}
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    event = envelope(
        "RUN.CANCEL_REQUESTED",
        pr_url=None,
        source_issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        source="issue_closed",
        reason="issue closed by alice",
    )
    out = handler(event, ctx())
    assert out["ok"] is True
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["event_type"] == "RUN.CANCEL_REQUESTED"
    assert body["issue_url"] == "https://github.com/smoketurner/ai-dlc/issues/9"
    assert body["pr_url"] == ""
    assert body["reason"] == "issue closed by alice"


@pytest.mark.parametrize(
    "event_type",
    [
        "ISSUE.TRIAGED",
        "DESIGN.READY",
        "CRITIQUE.READY",
        "REVIEW.READY",
        "CHECKS.PASSED",
        "CHECKS.FAILED",
        "IMPL_PR.OPENED",
        "IMPL.ITERATION_REQUESTED",
        "REVISION.READY",
    ],
)
def test_handler_ignores_non_terminal_events(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    """In-pipeline events are ignored — only RUN.* fires the retrospector."""
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope(event_type), ctx())
    assert out == {"ok": True, "ignored": event_type}
    fake.invoke_agent_runtime.assert_not_called()


@pytest.mark.parametrize(
    "event_type",
    ["SPEC.APPROVED", "SPEC.REJECTED", "TASK.APPROVED", "TASK.REJECTED"],
)
def test_handler_rejects_removed_event_types_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    """Pre-refactor event types fail envelope validation (Literal mismatch)."""
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope(event_type), ctx())
    assert out == {"ok": False, "error": "validation_error"}
    fake.invoke_agent_runtime.assert_not_called()


def test_handler_ignores_event_missing_project_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("RUN.COMPLETED", project_slug=""), ctx())
    assert out == {"ok": True, "ignored": "missing_fields"}
    fake.invoke_agent_runtime.assert_not_called()


def test_handler_ignores_event_with_neither_pr_nor_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope("RUN.COMPLETED", pr_url=""), ctx())
    assert out == {"ok": True, "ignored": "missing_fields"}
    fake.invoke_agent_runtime.assert_not_called()


def test_handler_returns_validation_error_for_malformed_envelope() -> None:
    out = handler({"source": "ai-dlc.test"}, ctx())
    assert out == {"ok": False, "error": "validation_error"}


def test_build_retrospector_input_extracts_target_repo_from_pr_url() -> None:
    """The agent input carries owner/name parsed out of the PR URL."""
    fake = SimpleNamespace(
        type="RUN.COMPLETED",
        run_id="r-1",
        correlation_id="c-1",
        payload={
            "project_slug": "ai-dlc",
            "pr_url": "https://github.com/o/r/pull/1",
        },
    )
    payload = build_retrospector_input(cast("Any", fake))
    assert payload is not None
    assert payload["target_repo"] == "o/r"
    assert payload["pr_url"] == "https://github.com/o/r/pull/1"
    # Removed fields no longer in the input.
    assert "spec_slug" not in payload
    assert "task_id" not in payload
    assert "reviewer" not in payload


def test_build_retrospector_input_enumerates_validation_artifacts_on_cap_hit() -> None:
    """``RUN.FAILED`` with ``revision_count`` enumerates per-round validator artifacts."""
    fake = SimpleNamespace(
        type="RUN.FAILED",
        run_id="01HJABCDEFGHIJKLMNOPQRSTUV",
        correlation_id="c-1",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/1",
            "failed_state": "validation_complete",
            "error_class": "RevisionCapReached",
            "error_message": "revision cap (3) hit",
            "retryable": False,
            "revision_count": 3,
        },
    )
    payload = build_retrospector_input(cast("Any", fake))
    assert payload is not None
    assert payload["revision_count"] == 3
    keys = payload["validation_artifact_keys"]
    # Four rounds (initial + 3 revisions) by 3 validator kinds = 12 keys.
    expected_round_count = 4
    expected_kind_count = 3
    assert len(keys) == expected_round_count * expected_kind_count
    run_id = "01HJABCDEFGHIJKLMNOPQRSTUV"
    assert f"runs/{run_id}/validation/reviewer-r0.md" in keys
    assert f"runs/{run_id}/validation/tester-r3.md" in keys
    assert f"runs/{run_id}/validation/code_critic-r2.md" in keys


def test_build_retrospector_input_keeps_only_documented_keys() -> None:
    """Pre-refactor payload fields should not leak through to the agent input."""
    fake = SimpleNamespace(
        type="RUN.COMPLETED",
        run_id="r-1",
        correlation_id="c-1",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/1",
            # Legacy keys callers may still emit by accident — must be dropped.
            "spec_slug": "ignored",
            "task_id": "T-1",
            "reviewer": "alice",
        },
    )
    payload = build_retrospector_input(cast("Any", fake))
    assert payload is not None
    expected_keys = {
        "event_type",
        "project_slug",
        "target_repo",
        "pr_url",
        "issue_url",
        "reason",
        "revision_count",
        "validation_artifact_keys",
        "run_id",
        "correlation_id",
        "actor_id",
    }
    assert set(payload.keys()) == expected_keys
