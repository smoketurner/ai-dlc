"""Tests for ``implementer.lint_gate.run_lint_gate``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from implementer.lint_gate import (
    _TIMEOUT_SECS,
    CommandResult,
    LintGateResult,
    run_lint_gate,
)

_COMMANDS = ("make lint", "make format", "make type", "make test")


def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestRunLintGatePass:
    """All four commands exit 0 → passed=True, four CommandResult entries."""

    def test_passed_true(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            result = run_lint_gate(tmp_path)

        assert result.passed is True
        assert len(result.commands) == 4
        assert all(r.exit_code == 0 for r in result.commands)
        assert mock_run.call_count == 4

    def test_command_names(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)):
            result = run_lint_gate(tmp_path)

        names = [r.command for r in result.commands]
        assert names == list(_COMMANDS)

    def test_cwd_is_path(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            run_lint_gate(tmp_path)

        for call in mock_run.call_args_list:
            assert call.kwargs["cwd"] == tmp_path

    def test_no_shell(self, tmp_path: Path) -> None:
        """Commands must not use shell=True (AC-004 — make targets, not raw uv)."""
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            run_lint_gate(tmp_path)

        for call in mock_run.call_args_list:
            assert call.kwargs.get("shell") is not True

    def test_timeout_param_passed(self, tmp_path: Path) -> None:
        """subprocess.run must be called with the 60-second timeout."""
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            run_lint_gate(tmp_path)

        for call in mock_run.call_args_list:
            assert call.kwargs.get("timeout") == _TIMEOUT_SECS

    def test_uses_make_not_uv(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)) as mock_run:
            run_lint_gate(tmp_path)

        for call in mock_run.call_args_list:
            args = call.args[0]
            assert args[0] == "make", f"expected 'make', got {args[0]!r}"

    def test_retry_count_default_zero(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)):
            result = run_lint_gate(tmp_path)

        assert result.retry_count == 0

    def test_retry_count_one(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)):
            result = run_lint_gate(tmp_path, retry_count=1)

        assert result.retry_count == 1

    def test_output_captured(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0, stdout="ok\n")):
            result = run_lint_gate(tmp_path)

        assert all(r.output == "ok\n" for r in result.commands)


class TestRunLintGateSingleFailure:
    """First command (make lint) fails; rest pass → passed=False."""

    def test_passed_false(self, tmp_path: Path) -> None:
        side_effects = [
            _make_proc(1, stderr="lint error"),
            _make_proc(0),
            _make_proc(0),
            _make_proc(0),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        assert result.passed is False

    def test_failing_command_recorded(self, tmp_path: Path) -> None:
        side_effects = [
            _make_proc(1, stderr="E101 bad indent"),
            _make_proc(0),
            _make_proc(0),
            _make_proc(0),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        lint_result = result.commands[0]
        assert lint_result.command == "make lint"
        assert lint_result.exit_code == 1
        assert "E101" in lint_result.output

    def test_passing_commands_still_run(self, tmp_path: Path) -> None:
        """All four commands run regardless of individual failures."""
        side_effects = [
            _make_proc(1),
            _make_proc(0),
            _make_proc(0),
            _make_proc(0),
        ]
        with patch("subprocess.run", side_effect=side_effects) as mock_run:
            run_lint_gate(tmp_path)

        assert mock_run.call_count == 4


class TestRunLintGateAllFailure:
    """All four commands fail → passed=False, all exit_codes non-zero."""

    def test_all_failed(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(1, stderr="fail")):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert all(r.exit_code != 0 for r in result.commands)

    def test_four_results(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(1)):
            result = run_lint_gate(tmp_path)

        assert len(result.commands) == 4


class TestRunLintGateTimeout:
    """subprocess.TimeoutExpired → exit_code=1, descriptive output, gate fails."""

    def test_timeout_treated_as_failure(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="make test", timeout=60),
        ):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert all(r.exit_code == 1 for r in result.commands)

    def test_timeout_output_message(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="make lint", timeout=60),
        ):
            result = run_lint_gate(tmp_path)

        assert all("timed out" in r.output for r in result.commands)

    def test_partial_timeout(self, tmp_path: Path) -> None:
        """Only make test times out; earlier commands pass."""
        side_effects = [
            _make_proc(0),
            _make_proc(0),
            _make_proc(0),
            subprocess.TimeoutExpired(cmd="make test", timeout=60),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            result = run_lint_gate(tmp_path)

        assert result.passed is False
        assert result.commands[3].exit_code == 1
        assert "timed out" in result.commands[3].output


class TestOutputTruncation:
    """Output longer than 4096 chars is silently truncated."""

    def test_output_truncated(self, tmp_path: Path) -> None:
        long_output = "x" * 8192
        with patch("subprocess.run", return_value=_make_proc(0, stdout=long_output)):
            result = run_lint_gate(tmp_path)

        for cmd_result in result.commands:
            assert len(cmd_result.output) <= 4096

    def test_stdout_stderr_combined(self, tmp_path: Path) -> None:
        with patch(
            "subprocess.run",
            return_value=_make_proc(1, stdout="out\n", stderr="err\n"),
        ):
            result = run_lint_gate(tmp_path)

        assert result.commands[0].output == "out\nerr\n"


class TestLintGateResultModel:
    """Structural constraints on the Pydantic models."""

    def test_immutable(self, tmp_path: Path) -> None:
        with patch("subprocess.run", return_value=_make_proc(0)):
            result = run_lint_gate(tmp_path)

        with pytest.raises(ValidationError):
            result.passed = False  # type: ignore[misc]

    def test_retry_count_must_be_0_or_1(self, tmp_path: Path) -> None:
        with (
            patch("subprocess.run", return_value=_make_proc(0)),
            pytest.raises(ValidationError),
        ):
            run_lint_gate(tmp_path, retry_count=2)

    def test_command_result_model(self) -> None:
        cr = CommandResult(command="make lint", exit_code=0, output="ok")
        assert cr.command == "make lint"
        assert cr.exit_code == 0
        assert cr.output == "ok"

    def test_lint_gate_result_model(self) -> None:
        cr = CommandResult(command="make lint", exit_code=0, output="")
        lgr = LintGateResult(passed=True, commands=[cr], retry_count=0)
        assert lgr.passed is True
        assert lgr.retry_count == 0
