"""Cross-SDK steering primitives — decisions, judges, and validators.

The platform has two agent runtime SDKs: Strands (Architect, Critic,
Code-Critic, Reviewer, Tester, Triage, Proposer, Retrospector) and the
Claude Agent SDK (Implementer). Each ships its own hook surface with
different types and signatures, but the *shape* of the work is the same:

* **Pre-tool steering** — inspect an intended tool call before it runs;
  allow, deny, redirect (rewrite input), or guide (short-circuit and
  tell the model what to do instead).
* **Post-tool judging** — inspect a tool's output (or the model's final
  response); accept it, or retry with a structured reason.

This module defines SDK-agnostic dataclasses for those decisions plus a
small library of pure validator functions agents can compose. The
SDK-specific ``HookProvider`` adapters that turn these into runnable
hooks live next to :class:`common.hooks.RequirePriorCall` and
:class:`common.hooks.ToolCallCounter` (Strands) or in the implementer's
local ``hooks.py`` (Claude Agent SDK).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---- Pre-tool decisions ---------------------------------------------------


@dataclass(frozen=True)
class Allow:
    """Let the tool call proceed unchanged."""


@dataclass(frozen=True)
class Deny:
    """Block the tool call outright; surface ``reason`` to the model."""

    reason: str


@dataclass(frozen=True)
class Redirect:
    """Rewrite the tool input to ``new_input`` before letting it proceed.

    ``reason`` is logged but not surfaced to the model — the model sees
    the rewritten call complete normally. Use this when there's a clear
    correct fix the model would otherwise have to discover by trial
    (e.g. an out-of-tree write path that should be rebased into ``cwd``).
    """

    new_input: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class Guide:
    """Short-circuit the call and tell the model what to do instead.

    Unlike :class:`Deny` (which rejects without prescriptive guidance),
    ``Guide`` is for cases where the next correct step is known and can
    be spelled out — e.g. "call ``read_memory_md`` first".
    """

    reason: str


type Decision = Allow | Deny | Redirect | Guide


# ---- Post-tool judgment ---------------------------------------------------


@dataclass(frozen=True)
class Accept:
    """The tool result (or model output) is acceptable as-is."""


@dataclass(frozen=True)
class Retry:
    """The result needs revision. ``reason`` is shown to the model."""

    reason: str


type JudgeResult = Accept | Retry


# ---- Generic validators ---------------------------------------------------

FILE_REF_PATTERN = re.compile(r"\b[\w./\-]+\.[A-Za-z0-9]+(?::\d+)?\b")


def validate_required_sections(
    text: str,
    sections: list[str],
    *,
    heading_level: int = 1,
) -> list[str]:
    """Check that every required section heading appears in ``text``.

    Section matching is case-insensitive and ignores leading/trailing
    whitespace inside the heading. Returns the list of missing section
    names in the same order as ``sections`` so callers can build a
    deterministic retry message.

    Args:
        text: Markdown body to scan.
        sections: Expected section heading texts.
        heading_level: Markdown heading level to look for. ``1`` matches
            ``#`` only; ``2`` matches ``##``; etc.

    Returns:
        Section names that were not found. Empty list = all present.
    """
    hashes = "#" * heading_level
    pattern = re.compile(rf"(?im)^\s{{0,3}}{re.escape(hashes)}\s+(.+?)\s*$")
    found = {m.group(1).strip().lower() for m in pattern.finditer(text)}
    return [s for s in sections if s.lower() not in found]


def validate_min_length(text: str, min_chars: int) -> list[str]:
    """Return one error if the trimmed ``text`` is shorter than ``min_chars``."""
    actual = len(text.strip())
    if actual < min_chars:
        return [f"output too short: {actual} chars, need at least {min_chars}"]
    return []


def validate_one_of(value: str, allowed: list[str]) -> list[str]:
    """Return one error if ``value`` is not in ``allowed``.

    The model occasionally surrounds the value with quotes, whitespace,
    or backtick fences; those are stripped before comparing. The match
    itself is otherwise exact (no case folding) so ``allowed`` should
    list the literal expected values.
    """
    cleaned = value.strip().strip("\"'`")
    if cleaned not in allowed:
        return [f"value {cleaned!r} not one of {allowed!r}"]
    return []


def validate_contains_file_ref(text: str) -> list[str]:
    """Return one error if ``text`` has no apparent file path reference.

    Matches paths of the form ``some/path/file.ext`` or
    ``some/path/file.ext:NN`` (with line number). Useful for review
    bodies where a ``request_changes`` verdict should anchor at least
    one finding to a specific location.
    """
    if FILE_REF_PATTERN.search(text):
        return []
    return ["output contains no file path reference"]
