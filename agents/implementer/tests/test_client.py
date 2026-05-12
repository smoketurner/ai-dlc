"""Tests for ``implementer.client.execute_task`` — new impl-branch merge flow."""

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
    merge_results: list[dict[str, Any]] | None = None,
    run_cancelled: bool = False,
) -> dict[str, list[Any]]:
    """Wire the side-effecting helpers in ``execute_task`` to fakes.

    Returns a dict mapping each side-effect helper name to a list that
    records the call args. Tests assert against these lists.
    """
    calls: dict[str, list[Any]] = {
        "clone_repo": [],
        "checkout_impl_branch": [],
        "fetch_spec": [],
        "create_branch": [],
        "write_blocked_md": [],
        "delete_blocked_md": [],
        "commit_changes": [],
        "push_branch": [],
        "invoke_repo_helper": [],
    }
    merges = list(merge_results or [{"merged": True, "merge_commit_sha": "deadbeef"}])

    def fake_commit_changes(msg: str) -> str:
        calls["commit_changes"].append(msg)
        return "deadbeef"

    def fake_invoke_repo_helper(**kw: Any) -> dict[str, Any]:
        calls["invoke_repo_helper"].append(kw)
        if kw.get("op") == "merge_branch" and merges:
            return merges.pop(0)
        return {}

    monkeypatch.setattr(client, "make_session", lambda **_: fake_session)
    monkeypatch.setattr(client, "spec_path", lambda: spec_dir)
    monkeypatch.setattr(client, "clone_repo", calls["clone_repo"].append)
    monkeypatch.setattr(client, "checkout_impl_branch", calls["checkout_impl_branch"].append)
    monkeypatch.setattr(client, "fetch_spec", calls["fetch_spec"].append)
    monkeypatch.setattr(client, "create_branch", calls["create_branch"].append)
    monkeypatch.setattr(
        client,
        "write_blocked_md",
        lambda **kw: calls["write_blocked_md"].append(kw),
    )
    monkeypatch.setattr(client, "delete_blocked_md", calls["delete_blocked_md"].append)
    monkeypatch.setattr(client, "commit_changes", fake_commit_changes)
    monkeypatch.setattr(client, "push_branch", calls["push_branch"].append)
    monkeypatch.setattr(client, "invoke_repo_helper", fake_invoke_repo_helper)
    monkeypatch.setattr(client, "short_diff_summary", lambda: "diff stat")
    monkeypatch.setattr(
        client,
        "agent_made_real_changes",
        lambda _slug, *, base: agent_made_real_changes,
    )
    monkeypatch.setattr(client, "has_uncommitted_changes", lambda: has_uncommitted_changes)
    monkeypatch.setattr(client, "run_cancelled", lambda _run_id: run_cancelled)
    monkeypatch.setattr(client, "unmerged_paths", lambda: [])
    monkeypatch.setattr(client, "abort_merge", lambda: None)

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
async def test_execute_task_no_diff_blocks_without_merge(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """Agent makes no real diff → write BLOCKED.md, push task branch, no merge call."""
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=None,
        agent_made_real_changes=False,
        has_uncommitted_changes=True,
    )

    result = await client.execute_task(payload)

    assert result.blocked_reason == "agent produced no diff"
    assert calls["write_blocked_md"]
    assert calls["write_blocked_md"][0]["task_id"] == "T-001"
    assert calls["commit_changes"] == ["T-001 (blocked): Add /healthz route"]
    assert calls["push_branch"] == ["aidlc/task/add-healthz/01999999-9999/t-001"]
    assert calls["invoke_repo_helper"] == []  # no merge attempted


@pytest.mark.asyncio
async def test_execute_task_blocked_from_finish_propagates(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """Agent reports ``status='blocked'`` — that reason wins; no merge call."""
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

    assert result.blocked_reason == "Spec was contradictory."
    assert calls["write_blocked_md"][0]["blocked_reason"] == "Spec was contradictory."
    assert calls["invoke_repo_helper"] == []


@pytest.mark.asyncio
async def test_execute_task_happy_path_merges_into_impl_branch(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """Successful run: commit, push, merge_branch → no blocked_reason."""
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

    assert result.blocked_reason is None
    assert calls["checkout_impl_branch"] == ["aidlc/impl/add-healthz/01999999-9999"]
    assert calls["create_branch"] == ["aidlc/task/add-healthz/01999999-9999/t-001"]
    assert calls["commit_changes"] == ["T-001: Add /healthz route"]
    assert calls["push_branch"] == ["aidlc/task/add-healthz/01999999-9999/t-001"]
    merges = [c for c in calls["invoke_repo_helper"] if c["op"] == "merge_branch"]
    assert len(merges) == 1
    assert merges[0]["base"] == "aidlc/impl/add-healthz/01999999-9999"
    assert merges[0]["head"] == "aidlc/task/add-healthz/01999999-9999/t-001"


@pytest.mark.asyncio
async def test_execute_task_run_cancelled_skips_merge(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """``run_cancelled=True`` short-circuits the merge with reason 'run cancelled'."""
    report = FinishReport(summary="Done.", status="done")
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=report,
        agent_made_real_changes=True,
        has_uncommitted_changes=True,
        run_cancelled=True,
    )

    result = await client.execute_task(payload)

    assert result.blocked_reason == "run cancelled"
    merges = [c for c in calls["invoke_repo_helper"] if c["op"] == "merge_branch"]
    assert merges == []


@pytest.mark.asyncio
async def test_execute_task_merge_404_surfaces_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """``merge_branch`` returns ``not_found=True`` → result is blocked, no resolver invoked."""
    report = FinishReport(summary="Done.", status="done")
    calls = install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=report,
        agent_made_real_changes=True,
        has_uncommitted_changes=True,
        merge_results=[{"merged": False, "not_found": True}],
    )

    result = await client.execute_task(payload)

    assert result.blocked_reason is not None
    assert "not_found" in result.blocked_reason
    merges = [c for c in calls["invoke_repo_helper"] if c["op"] == "merge_branch"]
    assert len(merges) == 1


@pytest.mark.asyncio
async def test_execute_task_merge_conflict_exhausts_to_blocked(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
    spec_dir_with_tasks: Path,
) -> None:
    """Two conflict responses without resolver progress → blocked."""
    report = FinishReport(summary="Done.", status="done")

    async def failing_resolver(*, impl_branch: str, attempt: int, run_id: str) -> bool:
        del impl_branch, attempt, run_id
        return False

    install_common_mocks(
        monkeypatch,
        fake_session=fake_session,
        spec_dir=spec_dir_with_tasks,
        drive_agent_report=report,
        agent_made_real_changes=True,
        has_uncommitted_changes=True,
        merge_results=[
            {"merged": False, "conflict": True},
            {"merged": False, "conflict": True},
            {"merged": False, "conflict": True},
        ],
    )
    monkeypatch.setattr(client, "resolve_conflict_with_agent", failing_resolver)
    monkeypatch.setattr(client, "abort_merge", lambda: None)
    monkeypatch.setattr(client, "unmerged_paths", lambda: [])

    result = await client.execute_task(payload)

    assert result.blocked_reason is not None
    assert "conflict" in result.blocked_reason
