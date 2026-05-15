"""Tests for ``implementer.lint_gate``."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from implementer.lint_gate import (
    MAX_REMEDIATION_PASSES,
    run_all_gate_commands,
    run_lint_gate,
    run_make_command,
)

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


def test_empty_output_handling(tmp_path: Path) -> None:
    """Empty stdout/stderr are handled correctly."""
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=None, stderr=None),
    ):
        returncode, output = run_make_command("make test", cwd=tmp_path)

    assert returncode == 1
    assert output == ""
    assert isinstance(output, str)


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


def test_stops_at_first_failing_command(tmp_path: Path) -> None:
    """Stops when the second command fails; third and fourth are not called."""
    call_log: list[str] = []

    def fake_run(cmd: str, cwd: Path) -> tuple[int, str]:
        call_log.append(cmd)
        if cmd == "make lint":
            return 1, "lint failed"
        return 0, ""

    with patch("implementer.lint_gate.run_make_command", side_effect=fake_run):
        passed, failing_command, output = run_all_gate_commands(tmp_path)

    assert passed is False
    assert failing_command == "make lint"
    assert output == "lint failed"
    assert "make test" in call_log
    assert "make lint" in call_log
    assert "make type" not in call_log
    assert "make format" not in call_log


# ---------------------------------------------------------------------------
# run_lint_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_commands_pass_first_try(tmp_path: Path) -> None:
    """Gate passes on the first attempt — no agent call needed."""
    drive_calls: list[str] = []

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        drive_calls.append(user_prompt)
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

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
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

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        drive_calls.append(user_prompt)
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

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
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


@pytest.mark.asyncio
async def test_output_truncation_for_large_failures(tmp_path: Path) -> None:
    """Large failure output is truncated to 8000 chars in the remediation prompt."""
    large_output = "x" * 10000
    captured_prompts: list[str] = []

    call_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "make test", large_output
        return True, "", ""

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        captured_prompts.append(user_prompt)
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=False),
    ):
        result = await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert result.passed is True
    assert len(captured_prompts) == 1
    assert "x" * 8000 in captured_prompts[0]
    assert "x" * 8001 not in captured_prompts[0]


@pytest.mark.asyncio
async def test_remediation_prompt_formatting(tmp_path: Path) -> None:
    """Remediation prompt includes command name, exit code context, and output."""
    captured_prompts: list[str] = []

    call_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "make lint", "error: unused import"
        return True, "", ""

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        captured_prompts.append(user_prompt)
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=False),
    ):
        await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "Command: make lint" in prompt
    assert "Exit code: non-zero" in prompt
    assert "error: unused import" in prompt
    assert "Do not open a PR" in prompt


@pytest.mark.asyncio
async def test_lint_gate_passes_correct_run_id_to_drive_agent(tmp_path: Path) -> None:
    """run_id is correctly passed to drive_agent_fn during remediation."""
    captured_run_ids: list[str] = []

    call_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "make test", "fail"
        return True, "", ""

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        captured_run_ids.append(run_id)
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=False),
    ):
        await run_lint_gate(run_id="test-run-123", drive_agent_fn=fake_drive)

    assert captured_run_ids == ["test-run-123"]


@pytest.mark.asyncio
async def test_lint_gate_handles_drive_agent_exception(tmp_path: Path) -> None:
    """Exception from drive_agent_fn during remediation propagates."""

    async def failing_drive(user_prompt: str, *, run_id: str) -> Any:
        raise RuntimeError("Agent session failed")

    with (
        patch(
            "implementer.lint_gate.run_all_gate_commands",
            return_value=(False, "make test", "test failed"),
        ),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=False),
        pytest.raises(RuntimeError, match="Agent session failed"),
    ):
        await run_lint_gate(run_id="r-1", drive_agent_fn=failing_drive)


@pytest.mark.asyncio
async def test_commit_message_includes_pass_number_and_command(tmp_path: Path) -> None:
    """Commit message includes pass number and failing command."""
    committed_messages: list[str] = []
    call_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return False, "make type", "type error"
        return True, "", ""

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=True),
        patch("implementer.lint_gate.commit_changes", side_effect=committed_messages.append),
    ):
        await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert len(committed_messages) == 1
    assert "lint-gate fix (pass 1): make type" in committed_messages[0]


@pytest.mark.asyncio
async def test_all_four_commands_pass_on_second_attempt(tmp_path: Path) -> None:
    """All four commands pass after remediation on second attempt."""
    attempt_count = 0

    def fake_all_gate(cwd: Path) -> tuple[bool, str, str]:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            return False, "make lint", "lint error"
        return True, "", ""

    async def fake_drive(user_prompt: str, *, run_id: str) -> Any:
        return None, {}

    with (
        patch("implementer.lint_gate.run_all_gate_commands", side_effect=fake_all_gate),
        patch("implementer.lint_gate.repo_path", return_value=tmp_path),
        patch("implementer.lint_gate.has_uncommitted_changes", return_value=True),
        patch("implementer.lint_gate.commit_changes", return_value="abc123"),
    ):
        result = await run_lint_gate(run_id="r-1", drive_agent_fn=fake_drive)

    assert result.passed is True
    assert result.attempts == 2
    assert result.last_failure is None
