"""Tests for ``implementer.repo_ops`` — diff inspection + PR draft override."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from implementer import repo_ops
from implementer.repo_ops import (
    RepoSession,
    agent_made_real_changes,
    changed_paths,
    draft_explanation,
    find_open_pr_for_branch,
    has_uncommitted_changes,
    open_pr,
    push_branch,
)


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


class FakeResponse:
    """A fake httpx response whose ``json`` returns a stashed payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None: ...

    def json(self) -> Any:
        return self.payload


def install_pr_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_pr_number: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replace ``httpx.Client`` with a fake that records POSTs and GETs.

    Returns ``(post_calls, get_calls)`` lists mutated as the requests
    happen. PR creation lands at index 0 of ``post_calls``; the
    explanatory comment (if the override engages) at index 1.

    GETs to ``/pulls`` (head/state filter) and ``/pulls/{n}`` are answered
    based on ``existing_pr_number``: ``None`` means "no PR exists" (empty
    list); a number means "this PR exists" so ``open_pr`` reuses it.
    """
    post_calls: list[dict[str, Any]] = []
    get_calls: list[dict[str, Any]] = []

    list_pulls_payload: list[dict[str, Any]] = (
        [] if existing_pr_number is None else [{"number": existing_pr_number}]
    )
    read_pull_payload: dict[str, Any] = {
        "html_url": f"https://github.com/owner/name/pull/{existing_pr_number}",
        "number": existing_pr_number,
    }
    post_payload: dict[str, Any] = {
        "html_url": "https://github.com/owner/name/pull/42",
        "number": 42,
    }

    class FakeClient:
        def __init__(self, *_: Any, **__: Any) -> None: ...

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_: Any) -> None: ...

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> FakeResponse:
            post_calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(post_payload)

        def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str] | None = None,
        ) -> FakeResponse:
            get_calls.append({"url": url, "headers": headers, "params": params})
            payload = list_pulls_payload if url.endswith("/pulls") else read_pull_payload
            return FakeResponse(payload)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return post_calls, get_calls


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
    calls, _ = install_pr_capture(monkeypatch)
    url = open_pr(session, branch="b", base="main", title="t", body="b")
    assert url == "https://github.com/owner/name/pull/42"
    assert len(calls) == 1
    assert calls[0]["json"]["draft"] is False


def test_open_pr_no_one_way_paths_honors_explicit_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["src/foo.py"])
    calls, _ = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=True)
    assert len(calls) == 1  # No comment when draft was requested up front
    assert calls[0]["json"]["draft"] is True


def test_open_pr_one_way_path_forces_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["src/foo.py", "terraform/envs/prod/main.tf"])
    calls, _ = install_pr_capture(monkeypatch)
    open_pr(session, branch="b", base="main", title="t", body="b", draft=False)
    assert calls[0]["json"]["draft"] is True


def test_open_pr_one_way_path_with_explicit_draft_stays_draft(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    fixed_diff(monkeypatch, ["migrations/0042_drop_email.sql"])
    calls, _ = install_pr_capture(monkeypatch)
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
    calls, _ = install_pr_capture(monkeypatch)
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
    calls, _ = install_pr_capture(monkeypatch)
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
    calls, _ = install_pr_capture(monkeypatch)
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


def test_push_branch_happy_path_uses_set_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_run_git(*args: str, cwd: Any = None) -> str:
        del cwd
        seen.append(args)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)
    push_branch("aidlc/spec/t-001")
    assert seen == [("push", "--set-upstream", "origin", "aidlc/spec/t-001")]


def test_push_branch_falls_back_to_force_with_lease_on_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject → fetch the remote branch → force-with-lease push it."""
    seen: list[tuple[str, ...]] = []

    def fake_run_git(*args: str, cwd: Any = None) -> str:
        del cwd
        seen.append(args)
        if args[0] == "push" and "--force-with-lease" not in args:
            msg = "git push failed (exit 1) ... ! [rejected] ... non-fast-forward"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)
    push_branch("aidlc/spec/t-001")
    assert seen == [
        ("push", "--set-upstream", "origin", "aidlc/spec/t-001"),
        ("fetch", "origin", "aidlc/spec/t-001"),
        ("push", "--force-with-lease", "origin", "aidlc/spec/t-001"),
    ]


def test_push_branch_propagates_force_push_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If even the force-with-lease push fails, surface the error."""

    def fake_run_git(*args: str, cwd: Any = None) -> str:
        del cwd
        if args[0] == "push":
            msg = f"git {args[0]} failed: simulated"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)
    with pytest.raises(RuntimeError, match="simulated"):
        push_branch("aidlc/spec/t-001")


def test_find_open_pr_for_branch_returns_none_when_empty(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    _, get_calls = install_pr_capture(monkeypatch)
    result = find_open_pr_for_branch(session, "aidlc/spec/t-001")
    assert result is None
    assert len(get_calls) == 1
    call = get_calls[0]
    assert call["url"] == "https://api.github.com/repos/owner/name/pulls"
    assert call["params"] == {"head": "owner:aidlc/spec/t-001", "state": "open"}


def test_find_open_pr_for_branch_returns_pr_number_when_present(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    install_pr_capture(monkeypatch, existing_pr_number=99)
    result = find_open_pr_for_branch(session, "aidlc/spec/t-001")
    assert result == 99


def test_open_pr_reuses_existing_pr_without_posting(
    monkeypatch: pytest.MonkeyPatch,
    session: RepoSession,
) -> None:
    """When an open PR already exists, return its URL — don't POST a new one."""
    fixed_diff(monkeypatch, ["src/foo.py"])
    post_calls, get_calls = install_pr_capture(monkeypatch, existing_pr_number=99)

    captured_logs: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def info(self, message: str, **kwargs: Any) -> None:
            captured_logs.append((message, kwargs))

        def warning(self, message: str, **kwargs: Any) -> None:
            captured_logs.append((message, kwargs))

    monkeypatch.setattr(repo_ops, "logger", FakeLogger())

    url = open_pr(session, branch="aidlc/spec/t-001", base="main", title="t", body="b")
    assert url == "https://github.com/owner/name/pull/99"
    assert post_calls == []  # No POST — we reused the existing PR
    # Two GETs: list /pulls (find_open_pr_for_branch) and /pulls/{n} (read_pr_html_url).
    assert len(get_calls) == 2
    assert any(msg == "open_pr reused existing pr" for msg, _ in captured_logs)


def test_has_uncommitted_changes_returns_false_when_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: "")
    assert has_uncommitted_changes() is False


def test_has_uncommitted_changes_returns_true_when_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: " M src/foo.py\n")
    assert has_uncommitted_changes() is True


def test_agent_made_real_changes_false_on_clean_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_diff(monkeypatch, [])
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: "")
    assert agent_made_real_changes("my-spec") is False


def test_agent_made_real_changes_false_when_only_spec_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_diff(monkeypatch, [])
    porcelain = " M docs/specs/my-spec/tasks.md\nA  docs/specs/my-spec/design.md\n"
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: porcelain)
    assert agent_made_real_changes("my-spec") is False


def test_agent_made_real_changes_true_when_other_paths_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_diff(monkeypatch, [])
    porcelain = " M docs/specs/my-spec/tasks.md\nA  src/feature.py\n"
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: porcelain)
    assert agent_made_real_changes("my-spec") is True


def test_agent_made_real_changes_handles_renames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`R  old -> new` lines should classify on the destination path."""
    fixed_diff(monkeypatch, [])
    porcelain = "R  docs/specs/my-spec/old.md -> src/new.py\n"
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: porcelain)
    assert agent_made_real_changes("my-spec") is True


def test_agent_made_real_changes_true_when_committed_outside_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PR #51: changes committed in a prior run on the same branch
    (or by the agent itself mid-session) must still register as real work even
    when the working tree is clean.
    """
    fixed_diff(monkeypatch, ["agents/implementer/src/implementer/lint_gate.py"])
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: "")
    assert agent_made_real_changes("my-spec") is True


def test_agent_made_real_changes_false_when_only_committed_spec_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec materialization on a no-op rerun: only ``docs/specs/<slug>/`` paths
    show up against ``origin/main`` and the working tree is clean.
    """
    fixed_diff(monkeypatch, ["docs/specs/my-spec/tasks.md", "docs/specs/my-spec/design.md"])
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: "")
    assert agent_made_real_changes("my-spec") is False


def test_agent_made_real_changes_true_when_committed_in_spec_uncommitted_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed sources: a committed spec-only path plus an uncommitted code edit
    still counts as real work via the uncommitted path.
    """
    fixed_diff(monkeypatch, ["docs/specs/my-spec/tasks.md"])
    monkeypatch.setattr(repo_ops, "run_git", lambda *_a, **_k: " M src/feature.py\n")
    assert agent_made_real_changes("my-spec") is True


def test_parse_pr_number_extracts_int() -> None:
    n = repo_ops.parse_pr_number("https://github.com/owner/repo/pull/42")
    assert n == 42


def test_parse_pr_number_rejects_non_pull_url() -> None:
    with pytest.raises(ValueError, match="unparseable"):
        repo_ops.parse_pr_number("https://github.com/owner/repo/issues/42")


def test_parse_pr_number_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="unparseable"):
        repo_ops.parse_pr_number("not a url")


def test_checkout_task_branch_runs_fetch_then_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch uses an explicit refspec into ``refs/remotes/origin/<branch>``.

    Regression: ``clone_repo`` shallow-clones with ``--branch main``
    which restricts the default fetch refspec to main. A bare
    ``git fetch origin <task-branch>`` only updates ``FETCH_HEAD`` —
    the subsequent ``checkout -B <branch> origin/<branch>`` then fails
    with "is not a commit" because the remote-tracking ref was never
    populated.
    """
    calls: list[tuple[str, ...]] = []

    def fake_run_git(*args: str, **_: Any) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(repo_ops, "run_git", fake_run_git)
    repo_ops.checkout_task_branch("aidlc/my-spec/t-001")
    assert calls[0] == (
        "fetch",
        "origin",
        "aidlc/my-spec/t-001:refs/remotes/origin/aidlc/my-spec/t-001",
    )
    assert calls[1][:2] == ("checkout", "-B")
    assert calls[1][2] == "aidlc/my-spec/t-001"
    assert calls[1][3] == "origin/aidlc/my-spec/t-001"
