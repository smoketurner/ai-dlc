"""Tests for ``implementer.gates``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from implementer import gates
from implementer.gates import (
    compose_remediation_prompt,
    run_all_gates,
    run_make_command,
    run_verification_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_proc(target: str = "test") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["make", target], 0, stdout="OK\n", stderr="")


def _fail_proc(
    target: str = "lint",
    msg: str = "E501 line too long",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["make", target], 1, stdout="", stderr=msg)


# ---------------------------------------------------------------------------
# run_make_command
# ---------------------------------------------------------------------------


def test_run_make_command_returns_completed_process(tmp_path: Path) -> None:
    with patch("implementer.gates.subprocess.run", return_value=_ok_proc()) as mock_run:
        proc = run_make_command("test", tmp_path, timeout=30)
    assert proc.returncode == 0
    mock_run.assert_called_once_with(
        [gates.MAKE_BIN, "test"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# run_all_gates
# ---------------------------------------------------------------------------


def test_all_gates_pass(tmp_path: Path) -> None:
    with (
        patch.object(gates, "_target_exists", return_value=True),
        patch.object(gates, "run_make_command", return_value=_ok_proc()),
    ):
        passed, failed, output = run_all_gates(tmp_path)
    assert passed is True
    assert failed == ""
    assert output == ""


def test_stops_at_first_failure(tmp_path: Path) -> None:
    call_log: list[str] = []

    def fake_run(
        target: str,
        _cwd: Path,
        *,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        call_log.append(target)
        if target == "lint":
            return _fail_proc("lint", "lint error")
        return _ok_proc(target)

    with (
        patch.object(gates, "_target_exists", return_value=True),
        patch.object(gates, "run_make_command", side_effect=fake_run),
    ):
        passed, failed, output = run_all_gates(tmp_path)

    assert not passed
    assert failed == "lint"
    assert "lint error" in output
    # type and format must NOT have been called
    assert "type" not in call_log
    assert "format" not in call_log


def test_skips_undefined_target(tmp_path: Path) -> None:
    def fake_exists(target: str, _cwd: Path) -> bool:
        return target != "type"

    call_log: list[str] = []

    def fake_run(
        target: str,
        _cwd: Path,
        *,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        call_log.append(target)
        return _ok_proc(target)

    with (
        patch.object(gates, "_target_exists", side_effect=fake_exists),
        patch.object(gates, "run_make_command", side_effect=fake_run),
    ):
        passed, _failed, _ = run_all_gates(tmp_path)

    assert passed
    assert "type" not in call_log


def test_timeout_treated_as_failure(tmp_path: Path) -> None:
    def fake_run(
        target: str,
        _cwd: Path,
        *,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        if target == "test":
            raise subprocess.TimeoutExpired(["make", "test"], timeout)
        return _ok_proc(target)

    with (
        patch.object(gates, "_target_exists", return_value=True),
        patch.object(gates, "run_make_command", side_effect=fake_run),
    ):
        passed, failed, output = run_all_gates(tmp_path)

    assert not passed
    assert failed == "test"
    assert "timed out" in output


# ---------------------------------------------------------------------------
# compose_remediation_prompt
# ---------------------------------------------------------------------------


def test_compose_remediation_prompt_contains_target_and_attempt() -> None:
    prompt = compose_remediation_prompt("lint", "E501 line too long\n", 2)
    assert "make lint" in prompt
    assert "attempt 2/" in prompt
    assert "E501 line too long" in prompt


# ---------------------------------------------------------------------------
# run_verification_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_gates_pass_first_try(tmp_path: Path) -> None:
    """All gates pass on the first attempt — drive_agent never called."""
    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", return_value=(True, "", "")) as mock_gates,
        patch.object(gates, "has_uncommitted_changes", return_value=False),
        patch.object(gates, "_remediate", new_callable=AsyncMock) as mock_rem,
    ):
        await run_verification_gate("run-1")

    mock_gates.assert_called_once()
    mock_rem.assert_not_called()


@pytest.mark.asyncio
async def test_all_gates_pass_first_try_commits_format_changes(tmp_path: Path) -> None:
    """All gates pass but there are uncommitted format changes — they get committed."""
    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", return_value=(True, "", "")),
        patch.object(gates, "has_uncommitted_changes", return_value=True),
        patch.object(gates, "commit_changes") as mock_commit,
        patch.object(gates, "_remediate", new_callable=AsyncMock),
    ):
        await run_verification_gate("run-1")

    mock_commit.assert_called_once()
    assert "format" in mock_commit.call_args[0][0]


@pytest.mark.asyncio
async def test_one_remediation_pass_then_pass(tmp_path: Path) -> None:
    """First gate attempt fails, drive_agent fixes it, second attempt passes."""
    gate_results = [
        (False, "lint", "lint error output"),
        (True, "", ""),
    ]

    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", side_effect=gate_results),
        patch.object(gates, "has_uncommitted_changes", return_value=False),
        patch.object(gates, "_remediate", new_callable=AsyncMock) as mock_rem,
    ):
        await run_verification_gate("run-2")

    mock_rem.assert_called_once()
    _run_id, failed, _output, attempt = mock_rem.call_args[0]
    assert failed == "lint"
    assert attempt == 1


@pytest.mark.asyncio
async def test_three_failures_writes_blocked_and_raises(tmp_path: Path) -> None:
    """After MAX_REMEDIATION_PASSES failures, writes BLOCKED.md and raises."""
    mcp = MagicMock()

    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", return_value=(False, "type", "type error")),
        patch.object(gates, "has_uncommitted_changes", return_value=False),
        patch.object(gates, "call_artifact_tool") as mock_artifact,
        patch.object(gates, "_remediate", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="make type"),
    ):
        await run_verification_gate("run-3", mcp_client=mcp)

    mock_artifact.assert_called_once()
    artifact_kwargs = mock_artifact.call_args[1]
    assert artifact_kwargs["op"] == "put_artifact"
    assert artifact_kwargs["key"] == "runs/run-3/BLOCKED.md"
    assert "type error" in artifact_kwargs["content"]


@pytest.mark.asyncio
async def test_three_failures_no_mcp_still_raises(tmp_path: Path) -> None:
    """BLOCKED.md write is skipped when mcp_client=None but RuntimeError still raised."""
    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", return_value=(False, "lint", "err")),
        patch.object(gates, "has_uncommitted_changes", return_value=False),
        patch.object(gates, "call_artifact_tool") as mock_artifact,
        patch.object(gates, "_remediate", new_callable=AsyncMock),
        pytest.raises(RuntimeError),
    ):
        await run_verification_gate("run-4", mcp_client=None)

    mock_artifact.assert_not_called()


@pytest.mark.asyncio
async def test_drive_agent_exception_during_remediation_continues(tmp_path: Path) -> None:
    """drive_agent raising during remediation is swallowed by _remediate; gate loops continue."""
    gate_results = [
        (False, "lint", "lint error"),
        (False, "lint", "lint error"),
        (False, "lint", "lint error"),
    ]

    mcp = MagicMock()

    async def exploding_drive_agent(_prompt: str, *, run_id: str) -> tuple[None, dict]:  # type: ignore[return]
        raise RuntimeError("SDK exploded")

    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", side_effect=gate_results),
        patch.object(gates, "has_uncommitted_changes", return_value=False),
        patch.object(gates, "call_artifact_tool"),
        # Patch drive_agent as seen through the lazy import inside _remediate.
        patch("implementer.client.drive_agent", exploding_drive_agent),
        # gate-blocked error surfaces; "SDK exploded" should not propagate.
        pytest.raises(RuntimeError, match="make lint"),
    ):
        await run_verification_gate("run-5", mcp_client=mcp)


@pytest.mark.asyncio
async def test_format_changes_committed_after_pass(tmp_path: Path) -> None:
    """When gates pass and has_uncommitted_changes is True, commit is called."""
    commit_calls: list[str] = []

    def fake_commit(msg: str) -> str:
        commit_calls.append(msg)
        return "abc123"

    with (
        patch.object(gates, "repo_path", return_value=tmp_path),
        patch.object(gates, "run_all_gates", return_value=(True, "", "")),
        patch.object(gates, "has_uncommitted_changes", return_value=True),
        patch.object(gates, "commit_changes", side_effect=fake_commit),
        patch.object(gates, "_remediate", new_callable=AsyncMock),
    ):
        await run_verification_gate("run-6")

    assert len(commit_calls) == 1
    assert "format" in commit_calls[0]
