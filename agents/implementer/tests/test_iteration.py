"""Tests for the iteration-mode helpers in ``implementer.client`` + ``app``."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from common.events import EventEnvelope, TaskReady
from common.runtime import (
    CiFailureFeedback,
    ImplementerInput,
    ImplementerResult,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)
from implementer.app import emit_task_ready
from implementer.client import (
    any_ci_failure_feedback,
    build_iteration_commit_message,
    compose_iteration_prompt,
    format_failed_check,
    format_feedback_item,
)


def make_input(
    *,
    iteration_count: int = 1,
    iteration_feedback: list[Any] | None = None,
    pr_url: str | None = "https://github.com/owner/repo/pull/42",
) -> ImplementerInput:
    return ImplementerInput.model_validate(
        {
            "project_slug": "demo",
            "spec_slug": "add-healthz",
            "spec_s3_prefix": "specs/add-healthz/",
            "task_id": "T-001",
            "run_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "target_repo": "owner/repo",
            "iteration_count": iteration_count,
            "iteration_feedback": iteration_feedback,
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


def test_format_review_comment_mention_no_line() -> None:
    item = ReviewCommentMentionFeedback(
        path="src/handler.py",
        commit_id="abcdef0",
        comment_id=7,
        body="x",
        commenter="alice",
    )
    out = format_feedback_item(item)
    assert "src/handler.py" in out
    assert ":" not in out.split("`src/handler.py`", 1)[1].split(" from")[0]


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


def test_format_failed_check_renders_summary() -> None:
    check = {
        "name": "CI / test",
        "conclusion": "failure",
        "html_url": "https://github.com/x/y/runs/1",
        "output": {"title": "fail", "summary": "1 of 50 failed"},
    }
    out = format_failed_check(check)
    assert "CI / test" in out
    assert "failure" in out
    assert "1 of 50 failed" in out


def test_format_failed_check_handles_missing_output() -> None:
    check = {"name": "CI", "conclusion": "failure", "html_url": "https://example.com"}
    out = format_failed_check(check)
    assert "(no summary)" in out


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


def test_build_iteration_commit_message_format() -> None:
    msg = build_iteration_commit_message("T-001", "Add /healthz", 2)
    assert msg == "T-001: iter 2 — Add /healthz"


def test_compose_iteration_prompt_lists_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub agent_memory_preamble to keep the test free of S3 lookups.
    monkeypatch.setattr("implementer.client.agent_memory_preamble", lambda **_: "<<MEMORY>>")
    payload = make_input(
        iteration_count=2,
        iteration_feedback=[
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
    prompt = compose_iteration_prompt(
        payload,
        task_title="Add /healthz",
        task_done_when="200 returns under 100ms",
        failed_checks=[],
    )
    assert "iteration 2" in prompt
    assert "T-001" in prompt
    assert "Add /healthz" in prompt
    assert payload.pr_url is not None
    assert payload.pr_url in prompt
    assert "CI / test" in prompt
    assert "src/handler.py" in prompt
    assert "<<MEMORY>>" in prompt
    assert "inline_replies" in prompt
    assert "do NOT create a new branch" in prompt


def test_compose_iteration_prompt_omits_failed_checks_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("implementer.client.agent_memory_preamble", lambda **_: "")
    payload = make_input(iteration_feedback=[])
    prompt = compose_iteration_prompt(
        payload, task_title="x", task_done_when=None, failed_checks=[]
    )
    assert "Failed CI check details" not in prompt


def test_emit_task_ready_builds_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[EventEnvelope[Any]] = []
    monkeypatch.setattr("implementer.app.publish", captured.append)
    payload = make_input(
        iteration_count=2,
        iteration_feedback=[
            ReviewCommentMentionFeedback(
                path="src/x.py",
                commit_id="abcdef0",
                comment_id=7,
                body="x",
                commenter="alice",
            ),
            IssueCommentMentionFeedback(comment_id=8, body="y", commenter="bob"),
        ],
    )
    result = ImplementerResult(
        task_id="T-001",
        pr_url=payload.pr_url,
        diff_summary="Fix null-check.",
        session_id="sess",
        token_in=1_000,
        token_out=200,
        cost_usd=0.005,
        duration_ms=15_000,
    )
    assert payload.pr_url is not None
    emit_task_ready(payload, result, pr_url=payload.pr_url)
    assert len(captured) == 1
    env = captured[0]
    assert env.type == "TASK.READY"
    assert env.actor_id == "implementer"
    assert isinstance(env.payload, TaskReady)
    assert env.payload.task_id == "T-001"
    assert env.payload.pr_url == payload.pr_url
