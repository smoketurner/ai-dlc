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


def _make_make_results(
    *,
    targets: tuple[str, ...] = ("test", "lint", "type", "format"),
    results: dict[str, tuple[int, str]],
) -> dict[str, tuple[int, str]]:
    """Build a target→(exit_code, output) map from a partial spec.

    Targets not present in ``results`` default to (0, "").
    """
    return {t: results.get(t, (0, "")) for t in targets}


def _fake_run_make(results_by_pass: list[dict[str, tuple[int, str]]]):
    """Return a callable that pops one pass-dict per call sequence.

    Each element of ``results_by_pass`` is a target→(exit_code, output)
    dict consumed in order across all ``run_make_command`` calls.
    """
    call_counter: dict[str, int] = {"pass": 0, "target_in_pass": 0}
    targets_order = ("test", "lint", "type", "format")

    per_pass_counters: list[int] = [0] * len(results_by_pass)

    def inner(target: str) -> tuple[int, str]:
        # Determine which pass we are in based on which targets have been
        # consumed already.  We track this by walking the pass list and
        # checking if the current call number matches a boundary.
        nonlocal call_counter
        current_pass = call_counter["pass"]
        if current_pass >= len(results_by_pass):
            # Fallback: all passes succeeded.
            return (0, "")
        result = results_by_pass[current_pass].get(target, (0, ""))
        per_pass_counters[current_pass] += 1
        if target == "format" or result[0] != 0:
            # End of pass (either completed all 4, or bailed on failure).
            call_counter["pass"] += 1
        return result

    return inner


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
    """All four targets exit 0 on the first pass → agent never called, returns None."""
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
    assert mock_make.call_args_list == [call("test"), call("lint"), call("type"), call("format")]


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

    # First pass: test ok, lint fails, type+format never reached.
    # Second pass: all ok.
    call_seq: list[tuple[int, str]] = [
        (0, ""),   # test pass 1
        (1, "lint error"),  # lint pass 1 → failure triggers remediation
        (0, ""),   # test pass 2
        (0, ""),   # lint pass 2
        (0, ""),   # type pass 2
        (0, ""),   # format pass 2
    ]
    seq_iter = iter(call_seq)

    def fake_make(target: str) -> tuple[int, str]:
        return next(seq_iter)

    commit_calls: list[str] = []

    def fake_commit(msg: str) -> str:
        commit_calls.append(msg)
        return "abc123"

    monkeypatch.setattr("implementer.gates.run_make_command", fake_make)
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
    monkeypatch.setattr("implementer.gates.commit_changes", fake_commit)

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

    # Every pass fails on "test".
    def fake_make(target: str) -> tuple[int, str]:
        if target == "test":
            return (1, "FAILED: 3 tests")
        return (0, "")

    monkeypatch.setattr("implementer.gates.run_make_command", fake_make)
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
    monkeypatch.setattr("implementer.gates.commit_changes", lambda msg: "abc")

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
    """First non-zero exit in a pass stops that pass immediately (no further commands)."""
    monkeypatch.setattr("implementer.gates.repo_path", lambda: tmp_path)
    (tmp_path / "Makefile").write_text("# stub\n")

    called_targets: list[str] = []

    # Pass 1: lint fails, type+format must NOT be called in the same pass.
    # Pass 2: all green so we exit cleanly.
    pass_num = {"n": 0}

    def fake_make(target: str) -> tuple[int, str]:
        called_targets.append(target)
        if pass_num["n"] == 0 and target == "lint":
            pass_num["n"] += 1  # first failure ends pass 1
            return (1, "lint failed")
        return (0, "")

    monkeypatch.setattr("implementer.gates.run_make_command", fake_make)
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", lambda: False)
    monkeypatch.setattr("implementer.gates.commit_changes", lambda msg: "abc")

    async def fake_drive(prompt: str, *, run_id: str) -> tuple[None, dict[str, Any]]:
        return None, {}

    await run_lint_gates("r-1", fake_drive)

    # Pass 1 must not include "type" or "format" after lint failed.
    pass1_targets = called_targets[: called_targets.index("lint") + 1]
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

    def fake_commit(msg: str) -> str:
        commit_calls.append(msg)
        return "abc123"

    # All targets pass, but format leaves uncommitted changes.
    uncommitted = {"flag": True}

    def fake_uncommitted() -> bool:
        return uncommitted["flag"]

    monkeypatch.setattr("implementer.gates.run_make_command", lambda target: (0, ""))
    monkeypatch.setattr("implementer.gates.has_uncommitted_changes", fake_uncommitted)
    monkeypatch.setattr("implementer.gates.commit_changes", fake_commit)

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
