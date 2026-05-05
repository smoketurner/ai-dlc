"""Strands hooks for the Reviewer.

Caps ``read_spec_doc`` at 3 calls per invocation. The Reviewer reads each
of the three spec documents (``requirements``, ``design``, ``tasks``) at
most once each; a 4th call indicates either a re-read on a cleared
context (rare, allowable as a 4th if we ever raise the cap) or a stuck
loop. Capping at 3 forces the agent to use what it already has.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import ToolCallCounter

READ_SPEC_DOC_CAP = 3


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"read_spec_doc": READ_SPEC_DOC_CAP}),
    ]
