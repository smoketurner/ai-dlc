"""Tests for ``implementer.quality_gate``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from implementer.quality_gate import (
    GateCommand,
    GateOutcome,
    GateResult,
    run_gate,
)

LINT_CMD = GateCommand(name="ruff-check", command="uv run ruff check .", category="lint")
FORMAT_CMD = GateCommand(
    name="ruff-format", command="uv run ruff format --check .", category="format"
)
TYPE_CMD = GateCommand(name="ty-check", command="uv run ty check", category="typecheck")


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_run_gate_all_pass(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    with patch("subprocess.run", return_value=_completed(0, "All good", "")) as mock_run:
        outcome = run_gate([LINT_CMD, FORMAT_CMD, TYPE_CMD], cwd=cwd)

    assert outcome.all_passed is True
    assert outcome.retry_prompt is None
    assert outcome.blocked_reason is None
    assert len(outcome.results) == 3
    assert all(r.passed for r in outcome.results)
    assert mock_run.call_count == 3


def test_run_gate_lint_fails(tmp_path: Path) -> None:
    cwd = str(tmp_path)

    def fake_run(cmd: str, **kwargs: object) -> MagicMock:
        if "ruff check" in cmd:
            return _completed(1, "E501 line too long\n", "")
        return _completed(0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        outcome = run_gate([LINT_CMD, FORMAT_CMD, TYPE_CMD], cwd=cwd)

    assert outcome.all_passed is False
    assert outcome.retry_prompt is not None
    assert outcome.blocked_reason is not None

    # retry_prompt must include the command name, exit code, and command string
    assert "ruff-check" in outcome.retry_prompt
    assert "exit 1" in outcome.retry_prompt
    assert LINT_CMD.command in outcome.retry_prompt
    assert "E501 line too long" in outcome.retry_prompt

    # only the failing result appears in retry_prompt
    assert "ruff-format" not in outcome.retry_prompt
    assert "ty-check" not in outcome.retry_prompt


def test_run_gate_multiple_failures(tmp_path: Path) -> None:
    with patch("subprocess.run", return_value=_completed(1, "bad", "")):
        outcome = run_gate([LINT_CMD, FORMAT_CMD, TYPE_CMD], cwd=str(tmp_path))

    assert outcome.all_passed is False
    assert outcome.retry_prompt is not None
    assert "ruff-check" in outcome.retry_prompt
    assert "ruff-format" in outcome.retry_prompt
    assert "ty-check" in outcome.retry_prompt


def test_run_gate_truncates_output(tmp_path: Path) -> None:
    long_output = "x" * 10000
    with patch("subprocess.run", return_value=_completed(1, long_output, "")):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert len(result.output) == 4096
    # truncation keeps the tail
    assert result.output == long_output[-4096:]


def test_run_gate_output_at_limit_not_truncated(tmp_path: Path) -> None:
    exact = "y" * 4096
    with patch("subprocess.run", return_value=_completed(0, exact, "")):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    assert outcome.results[0].output == exact


def test_run_gate_empty_commands_returns_all_passed(tmp_path: Path) -> None:
    outcome = run_gate([], cwd=str(tmp_path))
    assert outcome.all_passed is True
    assert outcome.results == ()
    assert outcome.retry_prompt is None
    assert outcome.blocked_reason is None


def test_run_gate_timeout_treated_as_failure(tmp_path: Path) -> None:
    exc = subprocess.TimeoutExpired(cmd="uv run ruff check .", timeout=60)
    exc.stdout = b"partial stdout"
    exc.stderr = b"partial stderr"

    with patch("subprocess.run", side_effect=exc):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    assert outcome.all_passed is False
    result = outcome.results[0]
    assert result.exit_code == 1
    assert "timed out" in result.output


def test_run_gate_timeout_bytes_decoded(tmp_path: Path) -> None:
    exc = subprocess.TimeoutExpired(cmd="uv run ruff check .", timeout=60)
    exc.stdout = b"\xff\xfe"  # bytes that need lossy decode
    exc.stderr = None

    with patch("subprocess.run", side_effect=exc):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    assert outcome.all_passed is False
    assert "timed out" in outcome.results[0].output


def test_run_gate_timeout_string_stdout(tmp_path: Path) -> None:
    exc = subprocess.TimeoutExpired(cmd="uv run ruff check .", timeout=60)
    exc.stdout = "string stdout"  # ty: ignore[invalid-assignment]
    exc.stderr = "string stderr"  # ty: ignore[invalid-assignment]

    with patch("subprocess.run", side_effect=exc):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    assert outcome.all_passed is False
    assert "string stdout" in outcome.results[0].output


def test_gate_result_passed_only_on_zero_exit(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    with patch("subprocess.run", return_value=_completed(0, "", "")):
        outcome = run_gate([LINT_CMD], cwd=cwd)
    assert outcome.results[0].passed is True

    with patch("subprocess.run", return_value=_completed(2, "", "")):
        outcome = run_gate([LINT_CMD], cwd=cwd)
    assert outcome.results[0].passed is False


def test_blocked_reason_includes_command_and_output(tmp_path: Path) -> None:
    with patch("subprocess.run", return_value=_completed(1, "lint error here", "")):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    assert outcome.blocked_reason is not None
    assert "ruff-check" in outcome.blocked_reason
    assert "lint error here" in outcome.blocked_reason
    assert "exit=1" in outcome.blocked_reason


def test_gate_commands_are_run_with_correct_cwd(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    cwd = str(tmp_path)

    def capture_run(cmd: str, **kwargs: object) -> MagicMock:
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return _completed(0, "", "")

    with patch("subprocess.run", side_effect=capture_run):
        run_gate([LINT_CMD], cwd=cwd)

    assert calls[0]["cwd"] == cwd


def test_gate_result_combines_stdout_and_stderr(tmp_path: Path) -> None:
    with patch("subprocess.run", return_value=_completed(1, "stdout text", "stderr text")):
        outcome = run_gate([LINT_CMD], cwd=str(tmp_path))

    combined = outcome.results[0].output
    assert "stdout text" in combined
    assert "stderr text" in combined


def test_gate_outcome_dataclass_immutable() -> None:
    result = GateResult(command=LINT_CMD, exit_code=0, output="", passed=True)
    with pytest.raises((AttributeError, TypeError)):
        result.exit_code = 1  # type: ignore[misc]  # ty: ignore[invalid-assignment]

    outcome = GateOutcome(
        results=(result,), all_passed=True, retry_prompt=None, blocked_reason=None
    )
    with pytest.raises((AttributeError, TypeError)):
        outcome.all_passed = False  # type: ignore[misc]  # ty: ignore[invalid-assignment]
