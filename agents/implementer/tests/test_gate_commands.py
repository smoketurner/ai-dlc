"""Tests for ``implementer.gate_commands``."""

from __future__ import annotations

import pytest

from common.stack_discovery import StackComponent, StackProfile
from implementer.gate_commands import (
    AIDLC_GATE_COMMANDS,
    GateCommand,
    resolve_gate_commands,
)


def _make_profile(
    *,
    lint_command: str | None = None,
    format_command: str | None = None,
    path: str = ".",
) -> StackProfile:
    component = StackComponent(
        path=path,
        language="python",
        package_manager="uv",
        manifest="pyproject.toml",
        lint_command=lint_command,
        format_command=format_command,
    )
    return StackProfile(components=(component,), primary_language="python")


# ---------------------------------------------------------------------------
# ai-dlc hardcoded defaults
# ---------------------------------------------------------------------------


def test_aidlc_self_commands_returned_without_profile_lookup() -> None:
    """project_slug == 'ai-dlc' returns hardcoded commands regardless of profile."""
    cmds = resolve_gate_commands("ai-dlc")
    assert len(cmds) == len(AIDLC_GATE_COMMANDS)
    names = [c.name for c in cmds]
    assert "ruff-check" in names
    assert "ruff-format" in names
    assert "ty-check" in names


def test_aidlc_self_commands_are_gate_commands() -> None:
    cmds = resolve_gate_commands("ai-dlc")
    assert all(isinstance(c, GateCommand) for c in cmds)


def test_aidlc_self_commands_categories() -> None:
    cmds = resolve_gate_commands("ai-dlc")
    by_name = {c.name: c for c in cmds}
    assert by_name["ruff-check"].category == "lint"
    assert by_name["ruff-format"].category == "format"
    assert by_name["ty-check"].category == "typecheck"


def test_aidlc_self_commands_ignore_supplied_profile() -> None:
    """When project_slug is ai-dlc, an explicit profile is ignored."""
    profile = _make_profile(lint_command="make lint")
    cmds = resolve_gate_commands("ai-dlc", profile=profile)
    # Should still return the hardcoded set
    assert len(cmds) == len(AIDLC_GATE_COMMANDS)


# ---------------------------------------------------------------------------
# External repo — StackProfile-derived commands
# ---------------------------------------------------------------------------


def test_external_repo_commands_from_profile() -> None:
    profile = _make_profile(
        lint_command="uv run ruff check .",
        format_command="uv run ruff format --check .",
    )
    cmds = resolve_gate_commands("some-external-repo", profile=profile)
    assert len(cmds) == 2
    assert cmds[0].category == "lint"
    assert cmds[0].command == "uv run ruff check ."
    assert cmds[1].category == "format"
    assert cmds[1].command == "uv run ruff format --check ."


def test_external_repo_lint_only_profile() -> None:
    profile = _make_profile(lint_command="npm run lint")
    cmds = resolve_gate_commands("node-project", profile=profile)
    assert len(cmds) == 1
    assert cmds[0].name == "lint"
    assert cmds[0].category == "lint"


def test_external_repo_format_only_profile() -> None:
    profile = _make_profile(format_command="cargo fmt --check")
    cmds = resolve_gate_commands("rust-project", profile=profile)
    assert len(cmds) == 1
    assert cmds[0].name == "format-check"
    assert cmds[0].category == "format"


def test_external_repo_no_root_component_returns_empty() -> None:
    """When the root component (path='.') is absent, return empty list."""
    profile = _make_profile(lint_command="npm run lint", path="packages/core")
    cmds = resolve_gate_commands("monorepo", profile=profile)
    assert cmds == []


def test_external_repo_root_component_no_commands_returns_empty() -> None:
    """Root component with no lint/format commands → empty list."""
    profile = _make_profile()
    cmds = resolve_gate_commands("bare-repo", profile=profile)
    assert cmds == []


# ---------------------------------------------------------------------------
# No profile available (S3 miss) — expect empty list
# ---------------------------------------------------------------------------


def test_resolve_gate_commands_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """When read_stack_profile returns None, return empty list (AC-007)."""
    monkeypatch.setattr("implementer.gate_commands.read_stack_profile", lambda _slug: None)
    cmds = resolve_gate_commands("unknown-project")
    assert cmds == []


def test_resolve_gate_commands_empty_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty StackProfile (no components) → empty list."""
    monkeypatch.setattr(
        "implementer.gate_commands.read_stack_profile",
        lambda _slug: StackProfile(),
    )
    cmds = resolve_gate_commands("no-manifests-repo")
    assert cmds == []


def test_resolve_gate_commands_uses_s3_when_no_explicit_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit profile, read_stack_profile is called with project_slug."""
    profile = _make_profile(lint_command="make lint")
    captured: list[str] = []

    def fake_read(slug: str) -> StackProfile:
        captured.append(slug)
        return profile

    monkeypatch.setattr("implementer.gate_commands.read_stack_profile", fake_read)
    cmds = resolve_gate_commands("my-project")
    assert captured == ["my-project"]
    assert len(cmds) == 1
    assert cmds[0].command == "make lint"
