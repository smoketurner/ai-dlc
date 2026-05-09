"""Unit tests for implementer.lint_gate."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from implementer.lint_gate import (
    _COMMANDS,
    _MAX_OUTPUT,
    LintGateResult,
    run_lint_gate,
)


def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestRunLintGatePass:
    def test_all_commands_pass(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            result = run_lint_gate(tmp_path)

        assert isinstance(result, LintGateResult)
        assert result.passed is True
        assert len(result.commands) == 3
        assert mock_run.call_count == 3

    def test_commands_invoked_with_correct_args(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            run_lint_gate(tmp_path)

        calls = mock_run.call_args_list
        invoked = [call.args[0] for call in calls]
        for cmd, args in zip(_COMMANDS, invoked, strict=True):
            assert args == cmd.split()

    def test_cwd_is_repo_path(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            run_lint_gate(tmp_path)

        for call in mock_run.call_args_list:
            assert call.kwargs["cwd"] == tmp_path

    def test_retry_count_stored(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)):
            result = run_lint_gate(tmp_path, retry_count=1)

        assert result.retry_count == 1

    def test_output_captured(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0, stdout="ok\n")):
            result = run_lint_gate(tmp_path)

        assert result.commands[0].output == "ok\n"

    def test_stdout_and_stderr_combined(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0, stdout="out", stderr="err")):
            result = run_lint_gate(tmp_path)

        assert result.commands[0].output == "outerr"


class TestRunLintGateSingleFailure:
    def test_first_command_fails(self, tmp_path: Path) -> None:
        side_effects = [
            _make_proc(1, stderr="E501 line too long"),
            _make_proc(0),
            _make_proc(0),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert result.commands[0].exit_code == 1
        assert result.commands[1].exit_code == 0
        assert result.commands[2].exit_code == 0

    def test_middle_command_fails(self, tmp_path: Path) -> None:
        side_effects = [
            _make_proc(0),
            _make_proc(1, stderr="would reformat"),
            _make_proc(0),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert result.commands[1].exit_code == 1

    def test_last_command_fails(self, tmp_path: Path) -> None:
        side_effects = [
            _make_proc(0),
            _make_proc(0),
            _make_proc(1, stderr="error[invalid-syntax]"),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert result.commands[2].exit_code == 1

    def test_failure_output_preserved(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            side_effect=[
                _make_proc(1, stderr="ruff: line too long"),
                _make_proc(0),
                _make_proc(0),
            ],
        ):
            result = run_lint_gate(tmp_path)

        assert "ruff: line too long" in result.commands[0].output


class TestRunLintGateAllFailure:
    def test_all_commands_fail(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(1, stderr="fail")):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert all(r.exit_code == 1 for r in result.commands)
        assert len(result.commands) == 3

    def test_all_three_commands_still_run(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(1)) as mock_run:
            run_lint_gate(tmp_path)

        assert mock_run.call_count == 3


class TestRunLintGateTimeout:
    def test_timeout_recorded_as_minus_one(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="uv", timeout=30),
        ):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert result.commands[0].exit_code == -1
        assert result.commands[0].output == "[timeout]"

    def test_timeout_on_one_command_runs_rest(self, tmp_path: Path) -> None:
        side_effects = [
            subprocess.TimeoutExpired(cmd="uv", timeout=30),
            _make_proc(0),
            _make_proc(0),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert result.commands[0].exit_code == -1
        assert result.commands[1].exit_code == 0
        assert result.commands[2].exit_code == 0

    def test_all_timeout(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="uv", timeout=30),
        ):
            result = run_lint_gate(tmp_path)

        assert all(r.exit_code == -1 for r in result.commands)


class TestOutputTruncation:
    def test_output_truncated_at_max(self, tmp_path: Path) -> None:
        big = "x" * (_MAX_OUTPUT + 100)
        with patch("subprocess.run", return_value=_make_proc(0, stdout=big)):
            result = run_lint_gate(tmp_path)

        assert len(result.commands[0].output) == _MAX_OUTPUT

    def test_output_within_limit_not_truncated(self, tmp_path: Path) -> None:
        small = "y" * 100
        with patch("subprocess.run", return_value=_make_proc(0, stdout=small)):
            result = run_lint_gate(tmp_path)

        assert result.commands[0].output == small


class TestCommandNames:
    def test_command_field_matches_command_string(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)):
            result = run_lint_gate(tmp_path)

        for cmd_str, cmd_result in zip(_COMMANDS, result.commands, strict=True):
            assert cmd_result.command == cmd_str
