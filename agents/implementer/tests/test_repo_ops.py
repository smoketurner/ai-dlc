"""Tests for ``implementer.repo_ops`` — diff inspection + PR draft override."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from implementer import repo_ops
from implementer.repo_ops import RepoSession, changed_paths, draft_explanation, open_pr


@pytest.fixture
def session() -> RepoSession:
    return RepoSession(
        target_repo="owner/name",
        access_token="ghs_test",  # noqa: S106 - fixture-only fake token
        author_login="ai-dlc[bot]",
        author_email="ai-dlc-bot@users.noreply.github.com",
        on_behalf_of_user=False,
    )


def fixed_diff(monkeypatch: pytest.MonkeyPatch, paths: list[str]) -> None:
    """Make ``changed_paths`` return ``paths`` regardless of args."""

    def fake_changed_paths(*, base: str) -> list[str]:
        del base
        return paths

    monkeypatch.setattr(repo_ops, "changed_paths", fake_changed_paths)


def install_pr_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``httpx.Client`` with a fake that records every POST it sees.

    Returns a list mutated as POSTs happen — the PR creation lands at index
    0; if the override engages, the explanatory comment lands at index 1.
    """
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {
                "html_url": "https://github.com/owner/name/pull/42",
                "number": 42,
            }

    class FakeClient:
        def __init__(self, *_: Any, **__: Any) -> None: ...

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_: Any) -> None: ...

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> FakeResponse:
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return calls


def test_changed_paths_parses_diff_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_git(*args: str, cwd: Any = None) -> str:
        del cwd, args
        return "src/foo.py\n\nterraform/envs/prod/main.tf\nREADME.md\n"

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)
    paths = changed_paths(base="main")
    assert paths == ["src/foo.py", "terraform/envs/prod/main.tf", "README.md"]


def test_changed_paths_runs_triple_dot_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_run_git(*args: str, cwd: Any = None) -> str:
        del cwd
        seen.append(args)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)
    changed_paths(base="main")
    assert seen == [("diff", "--name-only", "origin/main...HEAD")]


def test_open_pr_no_one_way_paths_honors_default_not_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["src/foo.py", "tests/test_foo.py"])
    calls = install_pr_capture(monkeypatch)
    url = open_pr(session, branch="b", base="main", title="t", body="b")
    assert url == "https://github.com/owner/name/pull/42"
    assert len(calls) == 1
    assert calls[0]["json"]["draft"] is False


def test_open_pr_no_one_way_paths_honors_explicit_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["src/foo.py"])
    calls = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=True)
    assert len(calls) == 1  # No comment when draft was requested up front
    assert calls[0]["json"]["draft"] is True


def test_open_pr_one_way_path_forces_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["src/foo.py", "terraform/envs/prod/main.tf"])
    calls = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=False)
    assert calls[0]["json"]["draft"] is True


def test_open_pr_one_way_path_with_explicit_draft_stays_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["migrations/0042_drop_email.sql"])
    calls = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=True)
    assert len(calls) == 1  # No override engaged → no comment
    assert calls[0]["json"]["draft"] is True


def test_open_pr_logs_when_forcing_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["terraform/modules/agents/iam.tf"])
    install_pr_capture(monkeypatch)

    captured_logs: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def warning(self, message: str, **kwargs: Any) -> None:
            captured_logs.append((message, kwargs))

    monkeypatch.setattr(repo_ops, "logger", FakeLogger())
    open_pr(session, branch="b", base="main", title="t", body="b", draft=False)
    assert len(captured_logs) == 1
    msg, kwargs = captured_logs[0]
    assert msg == "open_pr forcing draft mode"
    assert kwargs["categories"] == ["iam_authorization"]


def test_open_pr_passes_through_payload_fields(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["README.md"])
    calls = install_pr_capture(monkeypatch)
    open_pr(session, branch="aidlc/x/t-001", base="main", title="T-001: x", body="body text")
    assert calls[0]["url"] == "https://api.github.com/repos/owner/name/pulls"
    assert calls[0]["json"]["title"] == "T-001: x"
    assert calls[0]["json"]["body"] == "body text"
    assert calls[0]["json"]["head"] == "aidlc/x/t-001"
    assert calls[0]["json"]["base"] == "main"
    assert calls[0]["headers"]["Authorization"] == "Bearer ghs_test"


def test_open_pr_posts_explanatory_comment_when_override_engages(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["terraform/envs/prod/main.tf", "migrations/0001_init.sql"])
    calls = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=False)
    assert len(calls) == 2
    pr_call, comment_call = calls
    assert pr_call["url"] == "https://api.github.com/repos/owner/name/pulls"
    assert comment_call["url"] == "https://api.github.com/repos/owner/name/issues/42/comments"
    body = comment_call["json"]["body"]
    assert "production_terraform" in body
    assert "schema_migration" in body
    assert "draft" in body.lower()


def test_open_pr_no_comment_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["docs/README.md"])
    calls = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=False)
    assert len(calls) == 1


def test_draft_explanation_lists_all_categories() -> None:
    body = draft_explanation(["production_terraform", "iam_authorization"])
    assert "`production_terraform`" in body
    assert "`iam_authorization`" in body
    assert "Ready for review" in body


def test_draft_explanation_handles_single_category() -> None:
    body = draft_explanation(["schema_migration"])
    assert "`schema_migration`" in body
