"""Tests for ``implementer.client.execute_task`` — control flow around the agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from common.runtime import ImplementerInput
from implementer import client
from implementer.finish import FinishReport
from implementer.repo_ops import RepoSession


@pytest.fixture
def payload() -> ImplementerInput:
    return ImplementerInput(
        project_slug="ai-dlc",
        spec_slug="add-healthz",
        spec_s3_prefix="specs/add-healthz/",
        task_id="T-001",
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
    )


@pytest.fixture
def fake_session() -> RepoSession:
    return RepoSession(
        target_repo="owner/name",
        access_token="ghs_test",  # noqa: S106 - fixture-only fake token
        author_login="ai-dlc[bot]",
        author_email="ai-dlc-bot@users.noreply.github.com",
        on_behalf_of_user=False,
    )


@pytest.fixture
def spec_dir_with_tasks(tmp_path: Path) -> Path:
    """A tmp dir holding a minimal ``tasks.md`` the implementer can parse."""
    (tmp_path / "tasks.md").write_text(
        "- [ ] **T-001** — Add /healthz route\n"
        "  - **Implements:** AC-R-001-a\n"
        "  - **Touches:** `src/foo.py`\n"
        "  - **Done when:** curl /healthz returns 200\n",
        encoding="utf-8",
    )
    return tmp_path


def install_common_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_session: RepoSession,
    spec_dir: Path,
    drive_agent_report: FinishReport | None,
    agent_made_real_changes: bool,
    has_uncommitted_changes: bool,
) -> dict[str, list[Any]]:
    """Wire all the side-effecting helpers in ``execute_task`` to fakes.

    Returns a dict mapping each side-effect helper name to a list that
    records the call args. Tests assert against these lists.
    """
    calls: dict[str, list[Any]] = {
        "clone_repo": [],
        "fetch_spec": [],
        "create_branch": [],
        "materialize_spec_in_repo": [],
        "update_tasks_md": [],
        "write_blocked_md": [],
        "delete_blocked_md": [],
        "commit_changes": [],
        "push_branch": [],
        "open_pr": [],
    }

    def fake_commit_changes(msg: str) -> str:
        calls["commit_changes"].append(msg)
        return "deadbeef"

    def fake_open_pr(session: RepoSession, **kw: Any) -> str:
        calls["open_pr"].append({"session": session, **kw})
        return "https://github.com/owner/name/pull/42"

    def fake_update_tasks_md(task_id: str, slug: str) -> None:
        calls["update_tasks_md"].append((task_id, slug))

    def fake_write_blocked_md(**kw: Any) -> None:
        calls["write_blocked_md"].append(kw)

    monkeypatch.setattr(client, "make_session", lambda **_: fake_session)
    monkeypatch.setattr(client, "spec_path", lambda: spec_dir)
    monkeypatch.setattr(client, "clone_repo", calls["clone_repo"].append)
    monkeypatch.setattr(client, "fetch_spec", calls["fetch_spec"].append)
    monkeypatch.setattr(client, "create_branch", calls["create_branch"].append)
    monkeypatch.setattr(
        client, "materialize_spec_in_repo", calls["materialize_spec_in_repo"].append
    )
    monkeypatch.setattr(client, "update_tasks_md", fake_update_tasks_md)
    monkeypatch.setattr(client, "write_blocked_md", fake_write_blocked_md)
    monkeypatch.setattr(client, "delete_blocked_md", calls["delete_blocked_md"].append)
    monkeypatch.setattr(client, "commit_changes", fake_commit_changes)
    monkeypatch.setattr(client, "push_branch", calls["push_branch"].append)
    monkeypatch.setattr(client, "open_pr", fake_open_pr)
    monkeypatch.setattr(client, "short_diff_summary", lambda: "diff stat")
    monkeypatch.setattr(client, "agent_made_real_changes", lambda _slug: agent_made_real_changes)
    monkeypatch.setattr(client, "has_uncommitted_changes", lambda: has_uncommitted_changes)

    usage = {"token_in": 100, "token_out": 50, "cost_usd": 0.01, "duration_ms": 1234}

    async def fake_drive_agent(
        _prompt: str,
        *,
        run_id: str,
    ) -> tuple[FinishReport | None, dict[str, Any]]:
        del run_id
        return drive_agent_report, usage

    monkeypatch.setattr(client, "drive_agent", fake_drive_agent)
    return calls


@pytest.mark.asyncio
async def test_execute_task_blocked_no_diff_opens_draft_pr_with_blocked_md(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """No real diff path: write BLOCKED.md, commit, push, open *draft* PR.

    A draft PR carrying ``BLOCKED.md`` is the system's request for human
    guidance — commenting on it (existing webhook → TASK.ITERATION_REQUESTED)
    advances; closing it (existing webhook → TASK.REJECTED) ends the task.
    Spec materialization + tasks.md flip are skipped on this path.
    """
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=None,
        agent_made_real_changes=False,
        has_uncommitted_changes=True,
    )

    result = await client.execute_task(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/42"
    assert result.blocked_reason == "agent produced no diff"
    assert calls["materialize_spec_in_repo"] == []
    assert calls["update_tasks_md"] == []
    assert len(calls["write_blocked_md"]) == 1
    assert calls["write_blocked_md"][0]["blocked_reason"] == "agent produced no diff"
    assert calls["write_blocked_md"][0]["task_id"] == "T-001"
    assert calls["commit_changes"] == ["T-001 (blocked): Add /healthz route"]
    assert calls["push_branch"] == ["aidlc/add-healthz/t-001"]
    assert len(calls["open_pr"]) == 1
    assert calls["open_pr"][0]["draft"] is True
    assert "(blocked)" in calls["open_pr"][0]["title"]


@pytest.mark.asyncio
async def test_execute_task_uses_blocked_reason_from_finish_when_present(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """Agent reported ``status='blocked'`` via finish — its reason flows through."""
    report = FinishReport(
        summary="Couldn't proceed.",
        status="blocked",
        blocked_reason="Spec was contradictory.",
    )
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=report,
        agent_made_real_changes=False,
        has_uncommitted_changes=True,
    )

    result = await client.execute_task(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/42"
    assert result.blocked_reason == "Spec was contradictory."
    assert calls["write_blocked_md"][0]["blocked_reason"] == "Spec was contradictory."
    assert calls["open_pr"][0]["draft"] is True


@pytest.mark.asyncio
async def test_execute_task_normal_path_commits_pushes_opens_pr(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    report = FinishReport(summary="Added /healthz endpoint.", status="done")
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=report,
        agent_made_real_changes=True,
        has_uncommitted_changes=True,
    )

    result = await client.execute_task(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/42"
    assert result.blocked_reason is None
    assert calls["materialize_spec_in_repo"] == ["add-healthz"]
    assert calls["update_tasks_md"] == [("T-001", "add-healthz")]
    assert calls["commit_changes"] == ["T-001: Add /healthz route"]
    assert calls["push_branch"] == ["aidlc/add-healthz/t-001"]
    assert len(calls["open_pr"]) == 1


@pytest.mark.asyncio
async def test_execute_task_skips_commit_when_tree_clean_after_materialize(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """Re-run case: agent did real work earlier (already committed), platform
    materialization didn't add anything new — skip commit but still push and
    open/reuse the PR."""
    report = FinishReport(summary="Re-run; nothing new to add.", status="done")
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=report,
        agent_made_real_changes=True,  # tree dirty before materialize
        has_uncommitted_changes=False,  # tree clean after materialize
    )

    result = await client.execute_task(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/42"
    assert calls["commit_changes"] == []  # skipped
    assert calls["push_branch"] == ["aidlc/add-healthz/t-001"]
    assert len(calls["open_pr"]) == 1
