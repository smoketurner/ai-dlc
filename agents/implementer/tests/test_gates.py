"""Unit tests for implementer.gates."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from implementer.gates import (
    MAX_OUTPUT_BYTES,
    CommandResult,
    GateResult,
    _run_command,
    _tail,
    _target_exists,
    build_remediation_prompt,
    run_lint_gates,
)

# ---------------------------------------------------------------------------
# _target_exists
# ---------------------------------------------------------------------------


def test_target_exists_returns_true_when_make_exits_zero(tmp_path: Path) -> None:
    """make -n <target> exits 0 → target exists."""
    with patch("implementer.gates.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert _target_exists("test", cwd=tmp_path, timeout=30) is True
        mock_run.assert_called_once()


def test_target_exists_returns_false_when_make_exits_two(tmp_path: Path) -> None:
    """make -n <target> exits 2 → 'No rule to make target'."""
    with patch("implementer.gates.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=2)
        assert _target_exists("missing_target", cwd=tmp_path, timeout=30) is False


def test_target_exists_returns_false_on_file_not_found(tmp_path: Path) -> None:
    """No `make` binary → treat as missing, not an error."""
    with patch("implementer.gates.subprocess.run", side_effect=FileNotFoundError):
        assert _target_exists("test", cwd=tmp_path, timeout=30) is False


def test_target_exists_returns_false_on_timeout(tmp_path: Path) -> None:
    with patch(
        "implementer.gates.subprocess.run",
        side_effect=subprocess.TimeoutExpired("make", 1),
    ):
        assert _target_exists("test", cwd=tmp_path, timeout=1) is False


# ---------------------------------------------------------------------------
# _run_command
# ---------------------------------------------------------------------------


def test_run_command_captures_stdout_and_stderr(tmp_path: Path) -> None:
    with patch("implementer.gates.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hello\n", stderr="")
        result = _run_command("make test", cwd=tmp_path, timeout=30)
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.command == "make test"


def test_run_command_on_timeout_returns_exit_minus_one(tmp_path: Path) -> None:
    with patch(
        "implementer.gates.subprocess.run",
        side_effect=subprocess.TimeoutExpired("make test", 5),
    ):
        result = _run_command("make test", cwd=tmp_path, timeout=5)
    assert result.exit_code == -1
    assert "timed out" in result.stderr


def test_run_command_truncates_large_output(tmp_path: Path) -> None:
    big = "x" * (MAX_OUTPUT_BYTES + 1000)
    with patch("implementer.gates.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=big, stderr="")
        result = _run_command("make test", cwd=tmp_path, timeout=30)
    assert len(result.stdout) == MAX_OUTPUT_BYTES


# ---------------------------------------------------------------------------
# _tail
# ---------------------------------------------------------------------------


def test_tail_returns_full_text_when_short() -> None:
    assert _tail("abc") == "abc"


def test_tail_truncates_to_max_output_bytes() -> None:
    long = "y" * (MAX_OUTPUT_BYTES + 500)
    result = _tail(long)
    assert len(result) == MAX_OUTPUT_BYTES
    assert result == long[-MAX_OUTPUT_BYTES:]


# ---------------------------------------------------------------------------
# run_lint_gates
# ---------------------------------------------------------------------------


def _make_completed_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_lint_gates_all_pass(tmp_path: Path) -> None:
    """All four targets exist and exit 0 → passed=True, no failures."""
    # _target_exists returns True (exit 0), _run_command returns 0.
    with patch("implementer.gates.subprocess.run") as mock_run:
        mock_run.return_value = _make_completed_proc(0, stdout="ok\n")
        result = run_lint_gates(tmp_path)
    assert result.passed is True
    assert result.failures == []
    assert len(result.all_results) == 4  # test, lint, type, format


def test_run_lint_gates_stops_at_first_failure(tmp_path: Path) -> None:
    """First target fails — only that failure is returned; remaining commands are not run."""
    call_count = 0

    def side_effect(*args: object, **_kw: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # Existence check (make -n ...) → target exists
        if isinstance(args[0], list) and len(args[0]) == 3 and args[0][1] == "-n":
            return _make_completed_proc(0)
        # First actual command (make test) fails
        return _make_completed_proc(1, stderr="error")

    with patch("implementer.gates.subprocess.run", side_effect=side_effect):
        result = run_lint_gates(tmp_path)

    assert result.passed is False
    assert len(result.failures) == 1
    assert result.failures[0].command == "make test"
    # Only ran 1 actual command (stopped after the first failure)
    assert len(result.all_results) == 1


def test_run_lint_gates_skips_missing_make_target(tmp_path: Path) -> None:
    """A target missing from the Makefile (exit 2 from make -n) is skipped."""
    call_count = 0

    def side_effect(*args: object, **_kw: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # args[0] = ["make", "-n", target] for existence check
        # Treat "make test" as missing (return 2), rest present (return 0)
        if isinstance(args[0], list) and len(args[0]) == 3 and args[0][1] == "-n":
            target = args[0][2]
            if target == "test":
                return _make_completed_proc(2)  # missing
            return _make_completed_proc(0)  # present
        return _make_completed_proc(0)  # actual commands pass

    with patch("implementer.gates.subprocess.run", side_effect=side_effect):
        result = run_lint_gates(tmp_path)

    assert result.passed is True
    # Only 3 commands ran (test was skipped)
    assert len(result.all_results) == 3
    assert all(r.command != "make test" for r in result.all_results)


def test_run_lint_gates_returns_correct_exit_code(tmp_path: Path) -> None:
    """The CommandResult preserves the exit code from the process."""
    call_count = 0

    def side_effect(*args: object, **_kw: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if isinstance(args[0], list) and len(args[0]) == 3 and args[0][1] == "-n":
            return _make_completed_proc(0)  # all targets exist
        # make lint exits 2
        if isinstance(args[0], str) and "lint" in args[0]:
            return _make_completed_proc(2, stderr="lint error")
        return _make_completed_proc(0)

    with patch("implementer.gates.subprocess.run", side_effect=side_effect):
        result = run_lint_gates(tmp_path)

    assert result.passed is False
    lint_failures = [f for f in result.failures if f.command == "make lint"]
    assert len(lint_failures) == 1
    assert lint_failures[0].exit_code == 2


# ---------------------------------------------------------------------------
# build_remediation_prompt
# ---------------------------------------------------------------------------


def test_build_remediation_prompt_includes_command_and_output() -> None:
    failures = [
        CommandResult(command="make lint", exit_code=1, stdout="", stderr="E501 line too long"),
    ]
    result = GateResult(passed=False, failures=failures, all_results=failures)
    prompt = build_remediation_prompt(result)
    assert "make lint" in prompt
    assert "E501 line too long" in prompt
    assert "exit 1" in prompt


def test_build_remediation_prompt_format_committed_note() -> None:
    failures = [
        CommandResult(command="make format", exit_code=1, stdout="", stderr=""),
    ]
    result = GateResult(passed=False, failures=failures, all_results=failures)
    prompt = build_remediation_prompt(result, format_committed=True)
    assert "already been committed" in prompt


def test_build_remediation_prompt_format_not_committed_note() -> None:
    failures = [
        CommandResult(command="make format", exit_code=1, stdout="", stderr=""),
    ]
    result = GateResult(passed=False, failures=failures, all_results=failures)
    prompt = build_remediation_prompt(result, format_committed=False)
    assert "git add -A" in prompt


def test_build_remediation_prompt_multiple_failures() -> None:
    failures = [
        CommandResult(command="make lint", exit_code=1, stdout="lint out", stderr=""),
        CommandResult(command="make type", exit_code=1, stdout="", stderr="type err"),
    ]
    result = GateResult(passed=False, failures=failures, all_results=failures)
    prompt = build_remediation_prompt(result)
    assert "make lint" in prompt
    assert "make type" in prompt
    assert "lint out" in prompt
    assert "type err" in prompt
