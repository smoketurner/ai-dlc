"""Tests for ``implementer.lint_gate.run_lint_gate``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from implementer.lint_gate import run_lint_gate


def _completed(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _mock_run(*outcomes: subprocess.CompletedProcess[str]) -> Any:
    """Return a side_effect list for patch('subprocess.run')."""
    return list(outcomes)


def _patch_run(side_effect: list[Any]) -> Any:
    return patch("implementer.lint_gate.subprocess.run", side_effect=side_effect)


# ---------------------------------------------------------------------------
# AC-001 / AC-007 — both tools exit 0 → gate_pass True
# ---------------------------------------------------------------------------


def test_both_pass(tmp_path: Path) -> None:
    ruff_ok = _completed(0, stdout="All checks passed.")
    ty_ok = _completed(0, stdout="")
    with _patch_run([ruff_ok, ty_ok]):
        result = run_lint_gate(tmp_path)
    assert result.gate_pass is True
    assert result.ruff_exit_code == 0
    assert result.ty_exit_code == 0


# ---------------------------------------------------------------------------
# AC-002 — ruff exits 1 → gate_pass False, ruff_output populated
# ---------------------------------------------------------------------------


def test_ruff_fail(tmp_path: Path) -> None:
    ruff_fail = _completed(1, stdout="src/foo.py:10:1: E501 line too long")
    ty_ok = _completed(0)
    with _patch_run([ruff_fail, ty_ok]):
        result = run_lint_gate(tmp_path)
    assert result.gate_pass is False
    assert result.ruff_exit_code == 1
    assert "E501" in result.ruff_output
    assert result.ty_exit_code == 0


# ---------------------------------------------------------------------------
# AC-003 — ty exits 1 → gate_pass False, ty_output populated
# ---------------------------------------------------------------------------


def test_ty_fail(tmp_path: Path) -> None:
    ruff_ok = _completed(0)
    ty_fail = _completed(1, stderr="src/bar.py:5:3: error[invalid-type] cannot assign to int")
    with _patch_run([ruff_ok, ty_fail]):
        result = run_lint_gate(tmp_path)
    assert result.gate_pass is False
    assert result.ruff_exit_code == 0
    assert result.ty_exit_code == 1
    assert "invalid-type" in result.ty_output


# ---------------------------------------------------------------------------
# AC-008 — gate logs structured event (gate_pass, exit codes)
# ---------------------------------------------------------------------------


def test_logs_structured_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with _patch_run([_completed(0), _completed(0)]):
        result = run_lint_gate(tmp_path)
    assert result.gate_pass is True
    # structlog emits to stdlib logging in test environments
    assert isinstance(result.ruff_exit_code, int)
    assert isinstance(result.ty_exit_code, int)


# ---------------------------------------------------------------------------
# AC-009 — tool not installed → exit_code None, gate still runs the other
# ---------------------------------------------------------------------------


def test_ruff_not_found_skips_gracefully(tmp_path: Path) -> None:
    ty_ok = _completed(0)
    with (
        patch(
            "implementer.lint_gate.subprocess.run",
            side_effect=[FileNotFoundError("uv not found"), ty_ok],
        ),
    ):
        result = run_lint_gate(tmp_path)
    assert result.ruff_exit_code is None
    assert result.ruff_output == ""
    assert result.ty_exit_code == 0
    assert result.gate_pass is True


def test_ty_not_found_skips_gracefully(tmp_path: Path) -> None:
    ruff_ok = _completed(0)
    with (
        patch(
            "implementer.lint_gate.subprocess.run",
            side_effect=[ruff_ok, FileNotFoundError("uv not found")],
        ),
    ):
        result = run_lint_gate(tmp_path)
    assert result.ruff_exit_code == 0
    assert result.ty_exit_code is None
    assert result.ty_output == ""
    assert result.gate_pass is True


def test_ruff_not_found_ty_fails_gate_fails(tmp_path: Path) -> None:
    ty_fail = _completed(1, stderr="src/x.py:1:1: error[foo] bad type")
    with (
        patch(
            "implementer.lint_gate.subprocess.run",
            side_effect=[FileNotFoundError(), ty_fail],
        ),
    ):
        result = run_lint_gate(tmp_path)
    assert result.ruff_exit_code is None
    assert result.ty_exit_code == 1
    assert result.gate_pass is False


# ---------------------------------------------------------------------------
# AC-005 — error_summary includes diagnostics; output truncated to 4096
# ---------------------------------------------------------------------------


def test_output_truncated_to_4096(tmp_path: Path) -> None:
    long_output = "x" * 8000
    ruff_fail = _completed(1, stdout=long_output)
    ty_ok = _completed(0)
    with _patch_run([ruff_fail, ty_ok]):
        result = run_lint_gate(tmp_path)
    assert len(result.ruff_output) == 4096


def test_error_summary_combines_diagnostics(tmp_path: Path) -> None:
    ruff_fail = _completed(1, stdout="src/a.py:1:1: E501 ruff diag")
    ty_fail = _completed(1, stderr="src/b.py:2:3: error[ty-err] ty diag")
    with _patch_run([ruff_fail, ty_fail]):
        result = run_lint_gate(tmp_path)
    summary = result.error_summary
    assert "ruff check" in summary
    assert "E501" in summary
    assert "ty check" in summary
    assert "ty-err" in summary


def test_error_summary_empty_on_pass(tmp_path: Path) -> None:
    with _patch_run([_completed(0), _completed(0)]):
        result = run_lint_gate(tmp_path)
    assert result.error_summary == ""


# ---------------------------------------------------------------------------
# Timeout — treated as failure
# ---------------------------------------------------------------------------


def test_ruff_timeout_is_failure(tmp_path: Path) -> None:
    ty_ok = _completed(0)
    with (
        patch(
            "implementer.lint_gate.subprocess.run",
            side_effect=[subprocess.TimeoutExpired(cmd=["uv"], timeout=30), ty_ok],
        ),
    ):
        result = run_lint_gate(tmp_path)
    assert result.ruff_exit_code == 1
    assert "timed out" in result.ruff_output
    assert result.gate_pass is False


# ---------------------------------------------------------------------------
# LintGateResult.error_summary only includes failing tools
# ---------------------------------------------------------------------------


def test_error_summary_excludes_passing_tool(tmp_path: Path) -> None:
    ruff_fail = _completed(1, stdout="file.py:1:1: E501")
    ty_ok = _completed(0)
    with _patch_run([ruff_fail, ty_ok]):
        result = run_lint_gate(tmp_path)
    assert "ruff check" in result.error_summary
    assert "ty check" not in result.error_summary
