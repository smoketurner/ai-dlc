"""Strands hooks for the Tester.

Mirrors the Reviewer's cap on plan reads — ``get_artifact`` at most 2
times per invocation. Mapping plan steps to tests shouldn't require
re-reading the plan repeatedly. The op name matches the
gateway-routed ``artifact_tool`` operation; the reviewer and code-critic
use the same naming axis.
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
