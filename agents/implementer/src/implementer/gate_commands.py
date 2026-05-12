"""Resolve which gate commands to run for a given project.

``resolve_gate_commands`` is the single entry point. It returns the list of
:class:`GateCommand` items that :func:`quality_gate.run_gate` should execute.

Resolution order:

1. When ``project_slug == "ai-dlc"`` (this platform's own repo), return the
   three hardcoded commands that match the project's ``pyproject.toml`` lint
   targets (ruff-check, ruff-format, ty-check). These are always correct for
   ai-dlc regardless of what the StackProfile says.
2. Otherwise, read the :class:`~common.stack_discovery.StackProfile` for the
   project from S3 (via :func:`~common.memory_md.read_stack_profile`) and
   derive commands from the root component's ``lint_command`` and
   ``format_command`` slots.
3. When no profile is found, or the root component has no lint/format commands,
   return an empty list — the caller skips the gate entirely (AC-007).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.memory_md import read_stack_profile

if TYPE_CHECKING:
    from common.stack_discovery import StackProfile

AIDLC_PROJECT_SLUG = "ai-dlc"

AIDLC_GATE_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("ruff-check", "uv run ruff check .", "lint"),
    ("ruff-format", "uv run ruff format --check .", "format"),
    ("ty-check", "uv run ty check", "typecheck"),
)


@dataclass(frozen=True)
class GateCommand:
    """One lint/typecheck command to run as part of the quality gate."""

    name: str
    """Short identifier, e.g. ``"ruff-check"``."""
    command: str
    """Shell command string, e.g. ``"uv run ruff check ."``."""
    category: str
    """One of ``"lint"``, ``"format"``, ``"typecheck"``."""


def resolve_gate_commands(
    project_slug: str,
    profile: StackProfile | None = None,
) -> list[GateCommand]:
    """Return the gate commands appropriate for ``project_slug``.

    When ``profile`` is ``None``, the profile is fetched from S3 via
    :func:`~common.memory_md.read_stack_profile`.  Pass an explicit
    ``profile`` to skip the S3 fetch (e.g. in tests).

    Args:
        project_slug: Project identifier, e.g. ``"ai-dlc"``.
        profile: Optional pre-fetched :class:`~common.stack_discovery.StackProfile`.
            When omitted, the profile is read from S3.

    Returns:
        Ordered list of :class:`GateCommand` items to pass to
        :func:`quality_gate.run_gate`.  Empty when no commands can be
        determined — the gate is skipped in that case (AC-007).
    """
    if project_slug == AIDLC_PROJECT_SLUG:
        return [GateCommand(name=n, command=c, category=cat) for n, c, cat in AIDLC_GATE_COMMANDS]

    resolved_profile = profile if profile is not None else read_stack_profile(project_slug)
    if resolved_profile is None:
        return []

    return _commands_from_profile(resolved_profile)


def _commands_from_profile(profile: StackProfile) -> list[GateCommand]:
    """Extract lint/format commands from the root component of ``profile``.

    Only the root component (``path == "."``) is consulted — sub-package
    commands aren't reliable enough to use as a global gate.
    """
    root = next((c for c in profile.components if c.path == "."), None)
    if root is None:
        return []

    commands: list[GateCommand] = []
    if root.lint_command:
        commands.append(GateCommand(name="lint", command=root.lint_command, category="lint"))
    if root.format_command:
        commands.append(
            GateCommand(name="format-check", command=root.format_command, category="format")
        )
    return commands
