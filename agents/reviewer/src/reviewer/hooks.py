"""Strands hooks for the Reviewer.

Caps ``get_artifact`` at 2 calls per invocation. The Reviewer reads
the architect's plan at most once (twice if re-reading after a long
agent loop is justified). The op name matches the gateway-routed
``artifact_tool`` operation; critic uses the same naming axis.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import ToolCallCounter

GET_ARTIFACT_CAP = 2


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"get_artifact": GET_ARTIFACT_CAP}),
    ]
