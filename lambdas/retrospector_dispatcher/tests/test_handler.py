"""Tests for retrospector_dispatcher.handler — capture + consolidate routing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext

from retrospector_dispatcher import handler as dispatcher
from retrospector_dispatcher.handler import (
    build_capture_input,
    derive_target_repo,
    handler,
)


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_RETROSPECTOR_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::runtime/r-1")
    monkeypatch.setenv("AIDLC_PLATFORM_REPO", "smoketurner/ai-dlc")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    dispatcher.agentcore_client.cache_clear()
    dispatcher.ddb_client.cache_clear()


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


# --- target_repo URL parsing ----------------------------------------------


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


# --- capture-mode event dispatch ------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "RUN.COMPLETED",
        "RUN.FAILED",
        "RUN.CANCEL_REQUESTED",
        "IMPL_PR.OPENED",
        "REVIEW.READY",
        "CHECKS.PASSED",
        "CHECKS.FAILED",
        "IMPL.ITERATION_REQUESTED",
    ],
)
def test_handler_invokes_runtime_in_capture_mode_for_pr_signal_events(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    fake = MagicMock()
    fake.invoke_agent_runtime.return_value = {"statusCode": 200}
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    out = handler(envelope(event_type), ctx())
    assert out == {
        "ok": True,
        "dispatched": event_type,
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
    }
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["mode"] == "capture"
    assert body["event_type"] == event_type


def test_handler_hydrates_verdict_for_review_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    handler(envelope("REVIEW.READY", verdict="request_changes"), ctx())
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["verdict"] == "request_changes"


def test_handler_hydrates_pr_comment_body_for_human_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    handler(
        envelope(
            "IMPL.ITERATION_REQUESTED",
            pr_comment_body="@aidlc-bot the pagination helper exists; use it.",
        ),
        ctx(),
    )
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert "pagination helper exists" in body["pr_comment_body"]


def test_handler_invokes_runtime_for_run_cancel_with_issue_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake)
    event = envelope(
        "RUN.CANCEL_REQUESTED",
        pr_url=None,
        source_issue_url="https://github.com/smoketurner/ai-dlc/issues/9",
        reason="issue closed by alice",
    )
    handler(event, ctx())
    body = json.loads(fake.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["issue_url"] == "https://github.com/smoketurner/ai-dlc/issues/9"
    assert body["pr_url"] == ""
    assert body["reason"] == "issue closed by alice"


@pytest.mark.parametrize(
    "event_type",
    ["ISSUE.TRIAGED", "DESIGN.READY", "REVISION.READY"],
)
def test_handler_ignores_in_pipeline_events(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    """In-pipeline progress events don't fire the retrospector — only PR-signal events do."""
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


# --- consolidate-mode scheduled fanout ------------------------------------


def scan_pages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap items in a list of paginator-style pages."""
    return [{"Items": items}]


def test_handler_recognises_scheduled_consolidate_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentcore = MagicMock()
    fake_ddb = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(
        scan_pages(
            [
                {"project_slug": {"S": "ai-dlc"}, "target_repo": {"S": "smoketurner/ai-dlc"}},
                {"project_slug": {"S": "demo"}, "target_repo": {"S": "owner/demo"}},
            ],
        ),
    )
    fake_ddb.get_paginator.return_value = paginator
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake_agentcore)
    monkeypatch.setattr(dispatcher, "ddb_client", lambda: fake_ddb)
    monkeypatch.setenv("AIDLC_RUNS_TABLE", "test-runs")

    out = handler({"detail-type": "SCHEDULED.LESSONS_CONSOLIDATE", "detail": {}}, ctx())

    # Platform + 2 projects = 3 invocations.
    expected_invocations = 3
    assert out == {"ok": True, "dispatched_consolidates": expected_invocations}
    assert fake_agentcore.invoke_agent_runtime.call_count == expected_invocations


def test_scheduled_consolidate_first_invocation_targets_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentcore = MagicMock()
    fake_ddb = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(scan_pages([]))
    fake_ddb.get_paginator.return_value = paginator
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake_agentcore)
    monkeypatch.setattr(dispatcher, "ddb_client", lambda: fake_ddb)
    monkeypatch.setenv("AIDLC_RUNS_TABLE", "test-runs")

    handler({"detail-type": "SCHEDULED.LESSONS_CONSOLIDATE", "detail": {}}, ctx())

    body = json.loads(fake_agentcore.invoke_agent_runtime.call_args.kwargs["payload"])
    assert body["mode"] == "consolidate"
    assert body["destination"] == "platform"
    assert body["target_repo"] == "smoketurner/ai-dlc"


def test_scheduled_consolidate_dedupes_repeated_project_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_agentcore = MagicMock()
    fake_ddb = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(
        scan_pages(
            [
                {"project_slug": {"S": "ai-dlc"}, "target_repo": {"S": "smoketurner/ai-dlc"}},
                {"project_slug": {"S": "ai-dlc"}, "target_repo": {"S": "smoketurner/ai-dlc"}},
                {"project_slug": {"S": "ai-dlc"}, "target_repo": {"S": "smoketurner/ai-dlc"}},
            ],
        ),
    )
    fake_ddb.get_paginator.return_value = paginator
    monkeypatch.setattr(dispatcher, "agentcore_client", lambda: fake_agentcore)
    monkeypatch.setattr(dispatcher, "ddb_client", lambda: fake_ddb)
    monkeypatch.setenv("AIDLC_RUNS_TABLE", "test-runs")

    handler({"detail-type": "SCHEDULED.LESSONS_CONSOLIDATE", "detail": {}}, ctx())

    # Platform + one (deduped) project.
    expected_invocations = 2
    assert fake_agentcore.invoke_agent_runtime.call_count == expected_invocations


# --- build_capture_input contract -----------------------------------------


def test_build_capture_input_extracts_target_repo_from_pr_url() -> None:
    fake = SimpleNamespace(
        type="RUN.COMPLETED",
        run_id="r-1",
        correlation_id="c-1",
        payload={
            "project_slug": "ai-dlc",
            "pr_url": "https://github.com/o/r/pull/1",
        },
    )
    payload = build_capture_input(cast("Any", fake))
    assert payload is not None
    assert payload["mode"] == "capture"
    assert payload["target_repo"] == "o/r"
    assert payload["pr_url"] == "https://github.com/o/r/pull/1"


def test_build_capture_input_enumerates_validation_artifacts_on_cap_hit() -> None:
    fake = SimpleNamespace(
        type="RUN.FAILED",
        run_id="01HJABCDEFGHIJKLMNOPQRSTUV",
        correlation_id="c-1",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/1",
            "revision_count": 3,
        },
    )
    payload = build_capture_input(cast("Any", fake))
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


def test_build_capture_input_keeps_only_documented_keys() -> None:
    fake = SimpleNamespace(
        type="RUN.COMPLETED",
        run_id="r-1",
        correlation_id="c-1",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/o/r/pull/1",
            # Pre-refactor keys callers may still emit by accident — must be dropped.
            "spec_slug": "ignored",
            "task_id": "T-1",
            "reviewer": "alice",
        },
    )
    payload = build_capture_input(cast("Any", fake))
    assert payload is not None
    expected_keys = {
        "mode",
        "event_type",
        "project_slug",
        "target_repo",
        "pr_url",
        "issue_url",
        "reason",
        "verdict",
        "pr_comment_body",
        "revision_count",
        "validation_artifact_keys",
        "run_id",
        "correlation_id",
        "actor_id",
    }
    assert set(payload.keys()) == expected_keys
