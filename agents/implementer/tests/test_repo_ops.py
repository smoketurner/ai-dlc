"""Tests for ``implementer.repo_ops`` helpers — branch checkout fallbacks."""

from __future__ import annotations

import pytest

from implementer import repo_ops


def test_checkout_task_branch_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the task branch exists, fetch + checkout it directly."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(repo_ops, "run_git", lambda *args: calls.append(args) or "")

    repo_ops.checkout_task_branch("aidlc/task/demo/abc123/t-001")

    assert calls == [
        ("fetch", "origin", "aidlc/task/demo/abc123/t-001"),
        ("checkout", "-B", "aidlc/task/demo/abc123/t-001", "origin/aidlc/task/demo/abc123/t-001"),
    ]


def test_checkout_task_branch_raises_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing task branch + no fallback → propagate the fetch error."""

    def fake_run_git(*args: str) -> str:
        if args[0] == "fetch":
            msg = "git fetch failed (exit 128) ... couldn't find remote ref"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)

    with pytest.raises(RuntimeError, match="couldn't find remote ref"):
        repo_ops.checkout_task_branch("aidlc/task/demo/abc123/t-001")


def test_checkout_task_branch_recreates_from_impl_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task branch missing on origin → recreate from the impl branch HEAD.

    Reproduces the post-merge iteration case: the task PR was merged
    into the impl branch and GitHub auto-deleted the task branch. The
    impl branch still carries the task's commits; iteration should
    branch off impl HEAD and apply feedback on top.
    """
    calls: list[tuple[str, ...]] = []

    def fake_run_git(*args: str) -> str:
        calls.append(args)
        if args == ("fetch", "origin", "aidlc/task/demo/abc123/t-001"):
            msg = "git fetch failed (exit 128) ... couldn't find remote ref"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)

    repo_ops.checkout_task_branch(
        "aidlc/task/demo/abc123/t-001",
        impl_branch_fallback="aidlc/impl/demo/abc123",
    )

    assert calls == [
        ("fetch", "origin", "aidlc/task/demo/abc123/t-001"),
        ("fetch", "origin", "aidlc/impl/demo/abc123"),
        ("checkout", "-B", "aidlc/task/demo/abc123/t-001", "origin/aidlc/impl/demo/abc123"),
    ]


def test_checkout_task_branch_fallback_unused_when_fetch_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is dormant when the task branch exists on origin."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(repo_ops, "run_git", lambda *args: calls.append(args) or "")

    repo_ops.checkout_task_branch(
        "aidlc/task/demo/abc123/t-001",
        impl_branch_fallback="aidlc/impl/demo/abc123",
    )

    assert calls == [
        ("fetch", "origin", "aidlc/task/demo/abc123/t-001"),
        ("checkout", "-B", "aidlc/task/demo/abc123/t-001", "origin/aidlc/task/demo/abc123/t-001"),
    ]
