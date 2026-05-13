"""Tests for ``implementer.client.execute_implementation`` — single-PR flow."""

from __future__ import annotations

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
        run_id="01999999-9999-7999-9999-999999999999",
        correlation_id="01999999-9999-7999-9999-999999999998",
        target_repo="owner/name",
        mode="implementation",
        plan_s3_key="runs/01999999-9999-7999-9999-999999999999/plan.md",
        critique_s3_key="runs/01999999-9999-7999-9999-999999999999/critique.md",
        source_issue_url="https://github.com/owner/name/issues/42",
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


def install_implementation_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_session: RepoSession,
    drive_agent_report: FinishReport | None,
    made_real_changes: bool,
    has_uncommitted_changes: bool,
    pr_url: str = "https://github.com/owner/name/pull/77",
) -> dict[str, list[Any]]:
    """Wire the side-effecting helpers in ``execute_implementation`` to fakes."""
    calls: dict[str, list[Any]] = {
        "clone_repo": [],
        "create_branch": [],
        "fetch_plan_and_critique": [],
        "commit_changes": [],
        "push_branch": [],
        "invoke_repo_helper": [],
    }

    def fake_commit_changes(msg: str) -> str:
        calls["commit_changes"].append(msg)
        return "deadbeef"

    def fake_fetch(**kw: Any) -> None:
        calls["fetch_plan_and_critique"].append(kw)

    def fake_invoke_repo_helper(**kw: Any) -> dict[str, Any]:
        calls["invoke_repo_helper"].append(kw)
        if kw.get("op") == "open_pr":
            return {"pr_url": pr_url}
        return {}

    monkeypatch.setattr(client, "make_session", lambda **_: fake_session)
    monkeypatch.setattr(client, "clone_repo", calls["clone_repo"].append)
    monkeypatch.setattr(client, "create_branch", calls["create_branch"].append)
    monkeypatch.setattr(client, "fetch_plan_and_critique", fake_fetch)
    monkeypatch.setattr(client, "commit_changes", fake_commit_changes)
    monkeypatch.setattr(client, "push_branch", calls["push_branch"].append)
    monkeypatch.setattr(client, "invoke_repo_helper", fake_invoke_repo_helper)
    monkeypatch.setattr(client, "short_diff_summary", lambda: "diff stat")
    monkeypatch.setattr(client, "repo_made_real_changes", lambda: made_real_changes)
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
async def test_execute_implementation_happy_path_opens_pr(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Successful run: commit, push, open_pr; result carries the PR URL."""
    report = FinishReport(summary="Added /healthz endpoint.", status="done")
    calls = install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=True,
    )

    result = await client.execute_implementation(payload)

    assert result.pr_url == "https://github.com/owner/name/pull/77"
    assert calls["create_branch"] == ["aidlc/impl/01999999-9999-7999-9999-999999999999"]
    assert calls["fetch_plan_and_critique"][0]["plan_s3_key"] == payload.plan_s3_key
    assert calls["commit_changes"], "agent commit was skipped"
    assert calls["push_branch"] == ["aidlc/impl/01999999-9999-7999-9999-999999999999"]
    opens = [c for c in calls["invoke_repo_helper"] if c["op"] == "open_pr"]
    assert len(opens) == 1
    assert opens[0]["head"] == "aidlc/impl/01999999-9999-7999-9999-999999999999"
    assert opens[0]["base"] == "main"
    # PR body links the source issue so merging auto-closes it.
    assert "Closes https://github.com/owner/name/issues/42" in opens[0]["body"]


@pytest.mark.asyncio
async def test_execute_implementation_no_diff_raises(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Agent makes no real diff → RuntimeError; no PR opened."""
    install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=FinishReport(summary="Nothing to do.", status="done"),
        made_real_changes=False,
        has_uncommitted_changes=False,
    )

    with pytest.raises(RuntimeError, match="no diff"):
        await client.execute_implementation(payload)


@pytest.mark.asyncio
async def test_execute_implementation_blocked_finish_raises(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """Agent calls finish with status='blocked' → RuntimeError surfacing the reason."""
    report = FinishReport(
        summary="Could not proceed.",
        status="blocked",
        blocked_reason="Plan was contradictory.",
    )
    install_implementation_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=report,
        made_real_changes=True,
        has_uncommitted_changes=True,
    )

    with pytest.raises(RuntimeError, match="Plan was contradictory"):
        await client.execute_implementation(payload)


def test_render_pr_body_includes_summary_and_issue_link() -> None:
    """The PR body picks up the agent's summary and Closes <issue>."""
    report = FinishReport(
        summary="Added /healthz route + unit test.",
        files_changed=["app.py", "tests/test_health.py"],
        risks=["depends on FastAPI startup ordering"],
        status="done",
    )
    body = client.render_pr_body(
        report=report,
        run_id="r-1",
        source_issue_url="https://github.com/owner/name/issues/9",
    )
    assert "## Summary" in body
    assert "Added /healthz route" in body
    assert "Closes https://github.com/owner/name/issues/9" in body
    assert "- `app.py`" in body
    assert "## Residual risks" in body
    assert "Run: `r-1`" in body


def test_pr_title_falls_back_when_no_report() -> None:
    assert client.pr_title(report=None, run_id="01999999-9999-7999") == "aidlc: run 01999999"


def test_compose_implementation_prompt_mentions_plan_and_critique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt threads through plan_s3_key + critique_s3_key + issue URL."""
    monkeypatch.setattr("implementer.client.agent_memory_preamble", lambda **_: "<<MEMORY>>")
    payload = ImplementerInput(
        project_slug="demo",
        run_id="r-1",
        correlation_id="c-1",
        target_repo="owner/repo",
        mode="implementation",
        plan_s3_key="runs/r-1/plan.md",
        critique_s3_key="runs/r-1/critique.md",
        source_issue_url="https://github.com/owner/repo/issues/3",
    )
    prompt = client.compose_implementation_prompt(payload)
    assert "<<MEMORY>>" in prompt
    assert "runs/r-1/plan.md" in prompt
    assert "runs/r-1/critique.md" in prompt
    assert "https://github.com/owner/repo/issues/3" in prompt
    assert "/workspace/spec/plan.md" in prompt
    assert "high-severity finding" in prompt.lower()
