"""Strands hooks for the Retrospector.

Two rules guard the lesson-extraction pass:

1. ``write_memory_md`` is gated on a prior ``read_memory_md`` — the
   retrospector must see the current ``MEMORY.md`` before proposing
   updates to it. Otherwise the agent can wholesale-overwrite an
   established index it has never inspected.
2. ``get_artifact`` is capped at 4 calls per invocation. Reading the
   PR, related issue threads, and the prior plan accounts for at most
   a handful of fetches; a higher count is a stuck loop, not legitimate
   re-reading.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import RequirePriorCall, ToolCallCounter

GET_ARTIFACT_CAP = 4


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"get_artifact": GET_ARTIFACT_CAP}),
        RequirePriorCall(target="write_memory_md", prerequisite="read_memory_md"),
    ]
