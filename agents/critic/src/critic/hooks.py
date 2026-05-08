"""Strands hooks for the Critic.

Caps ``read_spec_doc`` at 3 calls per invocation (one per spec document).
A 4th call indicates a stuck loop, not legitimate re-reading.

The "Critic must find at least one issue" rule lives on
:class:`critic.critique.Critique` itself as a ``min_length=1`` constraint
on ``issues``. Strands' structured-output mode surfaces the resulting
Pydantic ``ValidationError`` to the agent, giving it a chance to
self-correct before the run completes.
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
