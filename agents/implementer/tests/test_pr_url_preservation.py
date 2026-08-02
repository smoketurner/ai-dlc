"""PR URL preservation when the success-path emit fails.

Regression for the bug where ``execute_implementation`` succeeds (the PR
exists on GitHub) but the subsequent ``emit_impl_pr_opened`` call raises
:class:`EventEmitError` (EventBridge rejected the ``IMPL_PR.OPENED``
entry). The broad ``except`` in ``run_implementer`` treated that as a
full run failure and called ``publish_run_failed(payload, exc)``, which
only read ``payload.pr_url`` — still ``None`` for an initial
implementation run — so the fallback ``RUN.FAILED`` event carried
``pr_url=""`` and the orphaned PR was lost from the event log.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from common.errors import EventEmitError
from common.events import EventEnvelope
from common.runtime import (
    ImplementerInput,
    ImplementerResult,
    ImplementerRevisionResult,
)
from implementer import app


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[EventEnvelope[Any]]:
    """Capture every envelope passed to ``publish`` or ``try_publish``."""
    out: list[EventEnvelope[Any]] = []
    monkeypatch.setattr(app, "publish", out.append)
    monkeypatch.setattr(app, "try_publish", out.append)
    return out


def make_impl_payload(
    *,
    pr_url: str | None = None,
    source_issue_url: str | None = "https://github.com/owner/name/issues/42",
) -> ImplementerInput:
    return ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="implementation",
        pr_url=pr_url,
        source_issue_url=source_issue_url,
    )


def make_revision_payload(pr_url: str) -> ImplementerInput:
    return ImplementerInput(
        project_slug="ai-dlc",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="revision",
        revision_number=1,
        pr_url=pr_url,
        source_issue_url="https://github.com/owner/name/issues/42",
    )


def make_impl_result(pr_url: str = "https://github.com/owner/name/pull/123") -> ImplementerResult:
    return ImplementerResult(
        pr_url=pr_url,
        diff_summary="Added feature X",
        session_id="test-session-id",
        token_in=1_000,
        token_out=200,
        cost_usd=0.005,
        duration_ms=15_000,
    )


def make_revision_result(
    pr_url: str = "https://github.com/owner/name/pull/123",
) -> ImplementerRevisionResult:
    return ImplementerRevisionResult(
        pr_url=pr_url,
        diff_summary="Fix null-check.",
        revision_number=2,
        session_id="test-session-id",
        token_in=2_000,
        token_out=300,
        cost_usd=0.012,
        duration_ms=42_000,
    )


def test_pr_url_preserved_from_result_when_emit_fails(
    captured: list[EventEnvelope[Any]],
) -> None:
    """Implementation succeeds but emit fails: PR URL is preserved from result.

    Previously this emitted ``pr_url=""`` because ``payload.pr_url`` is
    ``None`` for an initial implementation run and the result was not
    forwarded to ``publish_run_failed``.
    """
    payload = make_impl_payload()
    result = make_impl_result()

    with (
        patch.object(app, "execute_implementation", return_value=result),
        patch.object(app, "emit_impl_pr_opened") as mock_emit,
    ):
        mock_emit.side_effect = EventEmitError("EventBridge rejected event")
        app.run_implementer(payload, async_task_id=0)

    assert len(captured) == 1
    failed = captured[0].payload
    assert failed.pr_url == "https://github.com/owner/name/pull/123"
    assert failed.source_issue_url == "https://github.com/owner/name/issues/42"


def test_result_pr_url_takes_precedence_over_payload_pr_url(
    captured: list[EventEnvelope[Any]],
) -> None:
    """When both ``result.pr_url`` and ``payload.pr_url`` are set, the
    freshly-created PR URL from the result wins (it's the one that actually
    exists on GitHub after this run)."""
    payload = make_impl_payload(pr_url="https://github.com/owner/name/pull/100")
    result = make_impl_result(pr_url="https://github.com/owner/name/pull/200")

    with (
        patch.object(app, "execute_implementation", return_value=result),
        patch.object(app, "emit_impl_pr_opened") as mock_emit,
    ):
        mock_emit.side_effect = EventEmitError("nope")
        app.run_implementer(payload, async_task_id=0)

    failed = captured[0].payload
    assert failed.pr_url == "https://github.com/owner/name/pull/200"


def test_revision_pr_url_preserved_from_result_when_emit_fails(
    captured: list[EventEnvelope[Any]],
) -> None:
    """Revision mode symmetric coverage: when revision succeeds but emit
    fails, PR URL is preserved from the result."""
    payload = make_revision_payload(pr_url="https://github.com/owner/name/pull/77")
    result = make_revision_result(pr_url="https://github.com/owner/name/pull/77")

    with (
        patch.object(app, "execute_revision", return_value=result),
        patch.object(app, "emit_revision_ready") as mock_emit,
    ):
        mock_emit.side_effect = EventEmitError("EventBridge rejected event")
        app.run_implementer(payload, async_task_id=0)

    assert len(captured) == 1
    failed = captured[0].payload
    assert failed.failed_state == "revising"
    assert failed.pr_url == "https://github.com/owner/name/pull/77"


def test_revision_pr_url_falls_back_to_payload_when_result_is_none(
    captured: list[EventEnvelope[Any]],
) -> None:
    """If ``execute_revision`` itself raises (``result`` stays ``None``), the
    fallback still carries ``payload.pr_url`` — which for a revision dispatch
    is always populated by the state-router from ``IMPL_PR.OPENED`` history."""
    payload = make_revision_payload(pr_url="https://github.com/owner/name/pull/77")

    with patch.object(app, "execute_revision", side_effect=RuntimeError("git crash")):
        app.run_implementer(payload, async_task_id=0)

    assert len(captured) == 1
    failed = captured[0].payload
    assert failed.pr_url == "https://github.com/owner/name/pull/77"
    assert failed.error_class == "RuntimeError"


def test_execute_implementation_failure_emits_empty_pr_url(
    captured: list[EventEnvelope[Any]],
) -> None:
    """An implementation run that fails *before* the PR is opened still emits
    ``pr_url=""`` (no result to harvest from). This is the unchanged behavior
    and must not regress."""
    payload = make_impl_payload(source_issue_url=None)

    with patch.object(app, "execute_implementation", side_effect=RuntimeError("agent crash")):
        app.run_implementer(payload, async_task_id=0)

    assert len(captured) == 1
    failed = captured[0].payload
    assert failed.pr_url == ""
    assert failed.source_issue_url == ""
    assert failed.error_class == "RuntimeError"
    assert failed.failed_state == "implementer_running"
