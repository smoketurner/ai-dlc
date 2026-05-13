"""Tests for ``implementer.gates`` — deterministic lint/type/test gate loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import call, patch

import pytest

from implementer.gates import GatesBlockedError, run_lint_gates

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop_drive_agent(_prompt: str, *, run_id: str) -> tuple[None, dict[str, Any]]:
    return None, {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_run_all_pass_first_try(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """All four targets exit 0 on first pass → agent never called, returns None."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    (tmp_path / "Makefile").write_text("# stub\n")

    drive_calls: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> tuple[None, dict[str, Any]]:
        drive_calls.append(prompt)
        return None, {}

    with patch("implementer.gates.run_make_command", return_value=(0, "ok")) as mock_make:
        monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
        await run_lint_gates("r-1", fake_drive)

    assert not drive_calls, "agent should not be called when all targets pass"
    assert mock_make.call_count == 4
    assert mock_make.call_args_list == [
        call("test"),
        call("lint"),
        call("type"),
        call("format"),
    ]


@pytest.mark.asyncio
async def test_one_pass_remediation_fail_then_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """First pass fails on 'lint'; agent called once; second pass all green."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    (tmp_path / "Makefile").write_text("# stub\n")

    drive_calls: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> tuple[None, dict[str, Any]]:
        drive_calls.append(prompt)
        return None, {}

    # Pass 1: test ok, lint fails (stops pass).
    # Pass 2: all ok.
    call_seq: list[tuple[int, str]] = [
        (0, ""),  # test pass 1
        (1, "lint error"),  # lint pass 1 → failure triggers remediation
        (0, ""),  # test pass 2
        (0, ""),  # lint pass 2
        (0, ""),  # type pass 2
        (0, ""),  # format pass 2
    ]
    seq_iter = iter(call_seq)

    commit_calls: list[str] = []

    def fake_commit_1(msg: str) -> None:
        commit_calls.append(msg)

    monkeypatch.setattr("implementer.gates.run_make_command", lambda _t: next(seq_iter))
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
    monkeypatch.setattr("implementer.gates.commit_changes", fake_commit_1)

    await run_lint_gates("r-1", fake_drive)

    assert len(drive_calls) == 1, "agent should be called exactly once for remediation"
    assert "make lint" in drive_calls[0]
    assert "lint error" in drive_calls[0]


@pytest.mark.asyncio
async def test_exhausted_remediation_raises_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """All three passes fail → GatesBlockedError raised after exactly 3 drive calls."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    (tmp_path / "Makefile").write_text("# stub\n")

    drive_calls: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> tuple[None, dict[str, Any]]:
        drive_calls.append(prompt)
        return None, {}

    def fake_make(target: str) -> tuple[int, str]:
        if target == "test":
            return (1, "FAILED: 3 tests")
        return (0, "")

    monkeypatch.setattr("implementer.gates.run_make_command", fake_make)
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
    monkeypatch.setattr("implementer.gates.commit_changes", lambda _msg: None)

    with pytest.raises(GatesBlockedError) as exc_info:
        await run_lint_gates("r-1", fake_drive, max_passes=3)

    assert len(drive_calls) == 3
    err = exc_info.value
    assert err.command == "test"
    assert "FAILED" in err.output


@pytest.mark.asyncio
async def test_per_command_exit_code_handling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """First non-zero exit in a pass stops that pass immediately (no further commands run)."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    (tmp_path / "Makefile").write_text("# stub\n")

    called_targets: list[str] = []
    lint_failed: list[bool] = [False]

    def fake_make(target: str) -> tuple[int, str]:
        called_targets.append(target)
        if target == "lint" and not lint_failed[0]:
            lint_failed[0] = True
            return (1, "lint failed")
        return (0, "")

    monkeypatch.setattr("implementer.gates.run_make_command", fake_make)
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
    monkeypatch.setattr("implementer.gates.commit_changes", lambda _msg: None)

    await run_lint_gates("r-1", _noop_drive_agent)

    # Pass 1 must stop at lint; type and format must not be called in that pass.
    lint_index = called_targets.index("lint")
    pass1_targets = called_targets[: lint_index + 1]
    assert "type" not in pass1_targets
    assert "format" not in pass1_targets


@pytest.mark.asyncio
async def test_format_changes_committed_after_format_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """make format exits 0 but leaves uncommitted changes → commit_changes is called."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    (tmp_path / "Makefile").write_text("# stub\n")

    commit_calls: list[str] = []

    def fake_commit_2(msg: str) -> None:
        commit_calls.append(msg)

    monkeypatch.setattr("implementer.gates.run_make_command", lambda _t: (0, ""))
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: True)
    monkeypatch.setattr("implementer.gates.commit_changes", fake_commit_2)

    await run_lint_gates("r-1", _noop_drive_agent)

    assert commit_calls, "commit_changes must be called to stage formatting changes"
    assert any("formatting" in msg for msg in commit_calls)


@pytest.mark.asyncio
async def test_no_makefile_skips_gates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When Makefile is absent, gates are skipped without error."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    # No Makefile created in tmp_path.

    called: list[str] = []

    async def fake_drive(prompt: str, *, run_id: str) -> tuple[None, dict[str, Any]]:
        called.append(prompt)
        return None, {}

    with patch("implementer.gates.run_make_command") as mock_make:
        await run_lint_gates("r-1", fake_drive)

    assert not called
    mock_make.assert_not_called()
