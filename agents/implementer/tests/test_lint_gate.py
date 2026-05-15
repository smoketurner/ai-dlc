"""Tests for ``implementer.lint_gate``."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from common.runtime import ImplementerInput
from implementer import client
from implementer.finish import FinishReport
from implementer.lint_gate import (
    MAX_REMEDIATION_PASSES,
    LintGateResult,
    run_all_gate_commands,
    run_lint_gate,
    run_make_command,
)
from implementer.repo_ops import RepoSession

# ---------------------------------------------------------------------------
# run_make_command
# ---------------------------------------------------------------------------


def test_run_make_command_returns_zero_and_output(tmp_path: Path) -> None:
    """Zero returncode + combined stdout/stderr are returned."""
    returncode, output = run_make_command("echo hello", cwd=tmp_path)
    assert returncode == 0
    assert "hello" in output


def test_individual_command_failure_captured(tmp_path: Path) -> None:
    """Non-zero exit and stderr content are captured together."""
    returncode, output = run_make_command("sh -c 'echo boom >&2; exit 1'", cwd=tmp_path)
    assert returncode != 0
    assert "boom" in output


# ---------------------------------------------------------------------------
# run_all_gate_commands
# ---------------------------------------------------------------------------


def test_run_all_gate_commands_all_pass(tmp_path: Path) -> None:
    """When every command exits 0 the function returns (True, '', '')."""
    with patch("implementer.lint_gate.run_make_command", return_value=(0, "")):
        passed, failing_command, output = run_all_gate_commands(tmp_path)
    assert passed is True
    assert failing_command == ""
    assert output == ""


def test_run_all_gate_commands_stops_on_first_failure(tmp_path: Path) -> None:
    """Stops at the first non-zero exit; returns the failing command name."""
    call_log: list[str] = []

    def fake_run(cmd: str, cwd: Path) -> tuple[int, str]:
        call_log.append(cmd)
        if cmd == "make lint":
            return 1, "lint error"
        return 0, ""

    with patch("implementer.lint_gate.run_make_command", side_effect=fake_run):
        passed, failing_command, output = run_all_gate_commands(tmp_path)

    assert passed is False
    assert failing_command == "make lint"
    assert "lint error" in output
    # Commands after the failing one must not have been called.
    assert "make type" not in call_log
    assert "make format" not in call_log


# ---------------------------------------------------------------------------
# run_lint_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_commands_pass_first_try(tmp_path: Path) -> None:
    """Gate passes on the first attempt — no agent call needed."""
    drive_calls: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> Any:
        drive_calls.append(prompt)
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", return_value=(True, "", "")),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
    ):
        result = await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert result.passed is True
    assert result.attempts == 1
    assert result.last_failure is None
    assert drive_calls == []


@pytest.mark.asyncio
async def test_remediation_pass_after_one_failure(tmp_path: Path) -> None:
    """Fails on first attempt, agent fixes it, second attempt passes."""
    call_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "make lint", "lint error"
        return True, "", ""

    async def fake_drive(prompt: str, *, run_id: str) -> Any:
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=False),
    ):
        result = await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert result.passed is True
    assert result.attempts == 2
    assert result.last_failure is None


@pytest.mark.asyncio
async def test_exhausted_remediation_returns_failure(tmp_path: Path) -> None:
    """All MAX_REMEDIATION_PASSES attempts fail — result.passed is False."""
    drive_calls: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> Any:
        drive_calls.append(prompt)
        return None, {}

    with (
        patch(
            "implementer.lint_gate.run_all_gate_commands",
            return_value=(False, "make test", "test failure output"),
        ),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=False),
    ):
        result = await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert result.passed is False
    assert result.attempts == MAX_REMEDIATION_PASSES
    assert result.last_failure is not None
    assert "test failure output" in result.last_failure
    # drive_agent called MAX_REMEDIATION_PASSES - 1 times (last pass doesn't call agent)
    assert len(drive_calls) == MAX_REMEDIATION_PASSES - 1


@pytest.mark.asyncio
async def test_lint_gate_commits_agent_fixes(tmp_path: Path) -> None:
    """After the agent fixes something, uncommitted changes are committed."""
    call_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "make format", "format error"
        return True, "", ""

    commits: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> Any:
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=True),
        patch("implementer.lint_gate.commit_changes", side_effect=commits.append),
    ):
        result = await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert result.passed is True
    assert len(commits) == 1
    assert "make format" in commits[0]


# ---------------------------------------------------------------------------
# Integration with client.py
# ---------------------------------------------------------------------------


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
        access_token="ghs_test",  # noqa: S106
        author_login="ai-dlc[bot]",
        author_email="ai-dlc-bot@users.noreply.github.com",
        on_behalf_of_user=False,
    )


def _wire_client_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_session: RepoSession,
    drive_agent_report: FinishReport | None,
    lint_gate_result: LintGateResult,
    pr_url: str = "https://github.com/owner/name/pull/77",
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"push_branch": [], "run_lint_gate": []}

    fake_mcp = MagicMock()
    fake_mcp.__enter__.return_value = fake_mcp
    fake_mcp.__exit__.return_value = False

    monkeypatch.setattr(client, "gateway_mcp_client", lambda: fake_mcp)
    monkeypatch.setattr(client, "make_session", lambda **_: fake_session)
    monkeypatch.setattr(client, "clone_repo", lambda *_: None)
    monkeypatch.setattr(client, "create_branch", lambda *_: None)
    monkeypatch.setattr(client, "fetch_plan_and_critique", lambda *_, **__: None)
    monkeypatch.setattr(client, "commit_changes", lambda *_: "deadbeef")
    monkeypatch.setattr(client, "push_branch", calls["push_branch"].append)
    monkeypatch.setattr(client, "short_diff_summary", lambda: "stat")
    monkeypatch.setattr(client, "repo_made_real_changes", lambda: True)
    monkeypatch.setattr(client, "has_uncommitted_changes", lambda: True)
    monkeypatch.setattr(
        client,
        "invoke_repo_helper",
        lambda _mcp, **kw: {"pr_url": pr_url} if kw.get("op") == "open_pr" else {},
    )

    usage = {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 0}

    async def fake_drive(
        _prompt: str,
        *,
        run_id: str,
    ) -> tuple[FinishReport | None, dict[str, Any]]:
        del run_id
        return drive_agent_report, usage

    monkeypatch.setattr(client, "drive_agent", fake_drive)

    async def fake_run_lint_gate(*, run_id: str, drive_agent_fn: Any) -> LintGateResult:
        calls["run_lint_gate"].append(run_id)
        return lint_gate_result

    monkeypatch.setattr(client, "run_lint_gate", fake_run_lint_gate)
    return calls


@pytest.mark.asyncio
async def test_execute_implementation_calls_lint_gate(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """run_lint_gate is called before push_branch in the happy path."""
    push_order: list[str] = []

    calls = _wire_client_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=FinishReport(summary="Done.", status="done"),
        lint_gate_result=LintGateResult(passed=True, attempts=1, last_failure=None),
    )

    # Track order: gate first, push second.
    original_push = calls["push_branch"].append

    def ordered_push(branch: str) -> None:
        push_order.append(f"push:{branch}")
        original_push(branch)

    monkeypatch.setattr(client, "push_branch", ordered_push)

    async def fake_run_lint_gate(*, run_id: str, drive_agent_fn: Any) -> LintGateResult:
        push_order.append("gate")
        calls["run_lint_gate"].append(run_id)
        return LintGateResult(passed=True, attempts=1, last_failure=None)

    monkeypatch.setattr(client, "run_lint_gate", fake_run_lint_gate)

    await client.execute_implementation(payload)

    assert calls["run_lint_gate"] == ["01999999-9999-7999-9999-999999999999"]
    # Gate must appear before push in push_order.
    gate_idx = push_order.index("gate")
    push_idx = next(i for i, s in enumerate(push_order) if s.startswith("push:"))
    assert gate_idx < push_idx


@pytest.mark.asyncio
async def test_execute_implementation_raises_on_lint_gate_failure(
    monkeypatch: pytest.MonkeyPatch,
    payload: ImplementerInput,
    fake_session: RepoSession,
) -> None:
    """When run_lint_gate returns passed=False, RuntimeError is raised before push."""
    calls = _wire_client_mocks(
        monkeypatch,
        fake_session=fake_session,
        drive_agent_report=FinishReport(summary="Done.", status="done"),
        lint_gate_result=LintGateResult(passed=False, attempts=3, last_failure="make test: FAILED"),
    )

    with pytest.raises(RuntimeError, match="lint gate exhausted"):
        await client.execute_implementation(payload)

    assert calls["push_branch"] == [], "push_branch must not be called on gate failure"
