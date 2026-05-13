"""Strands hooks for the Reviewer.

Caps ``read_plan_doc`` at 2 calls per invocation. The Reviewer reads
the architect's plan at most once (twice if re-reading after a long
agent loop is justified). A higher count indicates a stuck loop.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import ToolCallCounter

READ_PLAN_DOC_CAP = 2


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"read_plan_doc": READ_PLAN_DOC_CAP}),
    ]
