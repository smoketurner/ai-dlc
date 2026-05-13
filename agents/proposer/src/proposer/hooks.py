"""Strands hooks for the Proposer.

The Proposer's failure mode is unbounded external research — it can
chain ``browse_url`` calls indefinitely while ignoring project context.
Two rules push the loop toward grounded, finite work:

1. ``browse_url`` is capped at 10 calls per invocation.
2. ``read_memory_md`` must be called before any ``browse_url`` —
   external research without project context tends to produce proposals
   that conflict with existing conventions.

``get_artifact`` is also capped at 4 (mirror of the Critic / Tester /
Code-Critic ceilings) so a stuck loop can't burn budget re-reading
the same artifact.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import RequirePriorCall, ToolCallCounter

BROWSE_URL_CAP = 10
GET_ARTIFACT_CAP = 4


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"browse_url": BROWSE_URL_CAP, "get_artifact": GET_ARTIFACT_CAP}),
        RequirePriorCall(target="browse_url", prerequisite="read_memory_md"),
    ]
