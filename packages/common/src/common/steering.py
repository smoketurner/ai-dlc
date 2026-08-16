"""Cross-SDK steering primitives — judges and validators.

The platform has two agent runtime SDKs: Strands (Architect, Critic,
Code-Critic, Reviewer, Tester, Triage, Proposer, Retrospector) and the
Claude Agent SDK (Implementer). Each ships its own hook surface with
different types and signatures, but the *shape* of the work is the same:

* **Pre-tool steering** — inspect an intended tool call before it runs;
  allow, deny, redirect (rewrite input), or guide (short-circuit and
  tell the model what to do instead).
* **Post-tool judging** — inspect a tool's output (or the model's final
  response); accept it, or retry with a structured reason.

This module defines SDK-agnostic dataclasses for post-tool judgments plus a
small library of pure validator functions agents can compose. The
SDK-specific ``HookProvider`` adapters that turn these into runnable
hooks live next to :class:`common.hooks.RequirePriorCall` and
:class:`common.hooks.ToolCallCounter` (Strands) or in the implementer's
local ``hooks.py`` (Claude Agent SDK).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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

FENCE_PATTERN = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")


def strip_code_fences(text: str) -> str:
    """Blank out fenced code blocks, preserving line structure.

    Markdown heading syntax is not special inside a fence, so a document
    that only *quotes* ``## Approach`` in a ```` ```markdown ```` block
    must not count as having that section. Lines are replaced rather
    than removed so any line-based reporting stays aligned.

    Handles both ``` and ~~~ fences, nested-looking fences of differing
    lengths, and an unclosed fence running to the end of the document.

    Args:
        text: Markdown body.

    Returns:
        ``text`` with every fenced-block line (and its delimiters)
        replaced by an empty line.
    """
    out: list[str] = []
    closing: str | None = None
    for line in text.splitlines():
        if closing is None:
            match = FENCE_PATTERN.match(line)
            if match is None:
                out.append(line)
                continue
            closing = match.group("fence")[0] * len(match.group("fence"))
            out.append("")
            continue
        out.append("")
        stripped = line.strip()
        # A closing fence is at least as long as the opener and nothing else.
        if stripped.startswith(closing) and set(stripped) == {closing[0]}:
            closing = None
    return "\n".join(out)


def validate_required_sections(
    text: str,
    sections: list[str],
    *,
    heading_level: int = 1,
) -> list[str]:
    """Check that every required section heading appears in ``text``.

    Headings quoted inside fenced code blocks do not count — otherwise a
    document that merely shows the expected template in a fence passes
    the gate while carrying none of the real sections.

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
    found = {m.group(1).strip().lower() for m in pattern.finditer(strip_code_fences(text))}
    return [s for s in sections if s.lower() not in found]
