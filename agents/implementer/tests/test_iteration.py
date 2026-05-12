"""Tests for the revision flow + emit helpers in ``implementer``."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from common.events import EventEnvelope, ImplPrOpened, RevisionReady
from common.runtime import (
    CiFailureFeedback,
    ImplementerInput,
    ImplementerResult,
    ImplementerRevisionResult,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)
from implementer.app import emit_impl_pr_opened, emit_revision_ready
from implementer.client import (
    any_ci_failure_feedback,
    compose_revision_prompt,
    format_feedback_item,
)


def make_input(
    *,
    mode: str = "revision",
    revision_number: int = 1,
    revision_feedback: list[Any] | None = None,
    pr_url: str | None = "https://github.com/owner/repo/pull/42",
) -> ImplementerInput:
    return ImplementerInput.model_validate(
        {
            "project_slug": "demo",
            "run_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "target_repo": "owner/repo",
            "mode": mode,
            "revision_number": revision_number,
            "revision_feedback": revision_feedback,
            "pr_url": pr_url,
        },
    )


def test_format_ci_failure_feedback() -> None:
    item = CiFailureFeedback(
        workflow_name="CI / test",
        conclusion="failure",
        head_sha="abcdef0",
        html_url="https://github.com/x/y/actions/runs/1",
    )
    out = format_feedback_item(item)
    assert "CI failure" in out
    assert "CI / test" in out
    assert "failure" in out
    assert "actions/runs/1" in out


def test_format_review_changes_requested_feedback() -> None:
    item = ReviewChangesRequestedFeedback(
        reviewer="alice",
        body="The null check is wrong.",
        review_id=99,
    )
    out = format_feedback_item(item)
    assert "Review requested changes" in out
    assert "@alice" in out
    assert "null check is wrong" in out


def test_format_review_changes_requested_handles_empty_body() -> None:
    item = ReviewChangesRequestedFeedback(reviewer="alice", review_id=99)
    out = format_feedback_item(item)
    assert "(no review body)" in out


def test_format_review_comment_mention_feedback() -> None:
    item = ReviewCommentMentionFeedback(
        path="src/handler.py",
        line=42,
        commit_id="abcdef0",
        comment_id=7,
        body="@ai-dlc[bot] this is wrong",
        commenter="alice",
    )
    out = format_feedback_item(item)
    assert "Inline comment" in out
    assert "src/handler.py" in out
    assert ":42" in out
    assert "@alice" in out
    assert "comment_id=7" in out
    assert "this is wrong" in out


def test_format_issue_comment_mention_feedback() -> None:
    item = IssueCommentMentionFeedback(
        comment_id=12,
        body="@ai-dlc[bot] take another look",
        commenter="bob",
    )
    out = format_feedback_item(item)
    assert "PR comment" in out
    assert "@bob" in out
    assert "comment_id=12" in out


def test_any_ci_failure_feedback_detects_ci() -> None:
    feedback: list[Any] = [
        IssueCommentMentionFeedback(comment_id=1, body="x", commenter="a"),
        CiFailureFeedback(
            workflow_name="CI",
            conclusion="failure",
            head_sha="abcdef0",
            html_url="https://x.example",
        ),
    ]
    assert any_ci_failure_feedback(feedback) is True


def test_any_ci_failure_feedback_returns_false_without_ci() -> None:
    feedback: list[Any] = [IssueCommentMentionFeedback(comment_id=1, body="x", commenter="a")]
    assert any_ci_failure_feedback(feedback) is False


def test_any_ci_failure_feedback_handles_none() -> None:
    assert any_ci_failure_feedback(None) is False


def test_compose_revision_prompt_lists_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revision prompt threads validator artifacts + per-revision feedback."""
    monkeypatch.setattr("implementer.client.agent_memory_preamble", lambda **_: "<<MEMORY>>")
    payload = make_input(
        revision_number=2,
        revision_feedback=[
            CiFailureFeedback(
                workflow_name="CI / test",
                conclusion="failure",
                head_sha="abcdef0",
                html_url="https://github.com/x/y/actions/runs/1",
            ),
            ReviewCommentMentionFeedback(
                path="src/handler.py",
                line=42,
                commit_id="abcdef0",
                comment_id=7,
                body="@ai-dlc[bot] please null-check",
                commenter="alice",
            ),
        ],
    )
    prompt = compose_revision_prompt(
        payload,
        revision_number=2,
        inputs={
            "review": "## Reviewer findings\n- bug X",
            "test_report": "## Tester findings\n- gap Y",
            "critique": "## Code-critic findings\n- missed edge Z",
            "mention": "@aidlc-bot also fix the typo",
            "checks": "workflow ci/test failed: 1 of 50 tests failed",
        },
    )
    assert "<<MEMORY>>" in prompt
    assert "Revision number: 2" in prompt
    assert "bug X" in prompt
    assert "gap Y" in prompt
    assert "missed edge Z" in prompt
    assert "@aidlc-bot also fix the typo" in prompt
    assert "ci/test failed" in prompt
    assert "CI / test" in prompt
    assert "src/handler.py" in prompt
    assert "directly on the impl branch" in prompt


def test_compose_revision_prompt_omits_optional_sources_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mention/checks artifacts → those sections do not appear."""
    monkeypatch.setattr("implementer.client.agent_memory_preamble", lambda **_: "")
    payload = make_input(revision_feedback=[])
    prompt = compose_revision_prompt(
        payload,
        revision_number=1,
        inputs={
            "review": "(none)",
            "test_report": "(none)",
            "critique": "(none)",
            "mention": "",
            "checks": "",
        },
    )
    assert "Human @aidlc-bot mention" not in prompt
    assert "CI failure context" not in prompt
    assert "Per-revision feedback items" not in prompt


def test_emit_impl_pr_opened_builds_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``emit_impl_pr_opened`` builds the IMPL_PR.OPENED envelope correctly."""
    captured: list[EventEnvelope[Any]] = []
    monkeypatch.setattr("implementer.app.publish", captured.append)
    payload = make_input(mode="implementation", revision_number=0, revision_feedback=None)
    result = ImplementerResult(
        pr_url="https://github.com/owner/repo/pull/77",
        diff_summary="Added /healthz route.",
        session_id="sess",
        token_in=1_000,
        token_out=200,
        cost_usd=0.005,
        duration_ms=15_000,
    )
    emit_impl_pr_opened(payload, result)
    assert len(captured) == 1
    env = captured[0]
    assert env.type == "IMPL_PR.OPENED"
    assert env.actor_id == "implementer"
    assert isinstance(env.payload, ImplPrOpened)
    assert env.payload.pr_url == "https://github.com/owner/repo/pull/77"
    assert env.payload.diff_summary == "Added /healthz route."


def test_emit_revision_ready_builds_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """``emit_revision_ready`` builds the REVISION.READY envelope correctly."""
    captured: list[EventEnvelope[Any]] = []
    monkeypatch.setattr("implementer.app.publish", captured.append)
    payload = make_input()
    result = ImplementerRevisionResult(
        pr_url="https://github.com/owner/repo/pull/77",
        diff_summary="Fix null-check.",
        revision_number=2,
        session_id="sess",
        token_in=2_000,
        token_out=300,
        cost_usd=0.012,
        duration_ms=42_000,
    )
    emit_revision_ready(payload, result)
    assert len(captured) == 1
    env = captured[0]
    assert env.type == "REVISION.READY"
    assert isinstance(env.payload, RevisionReady)
    assert env.payload.revision_number == 2
    assert env.payload.cost_usd == 0.012
