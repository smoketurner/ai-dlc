"""Strands hooks for the Tester.

Mirrors the Reviewer's cap on plan reads — ``read_plan_doc`` at most 2
times per invocation. Mapping plan steps to tests shouldn't require
re-reading the plan repeatedly.
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
