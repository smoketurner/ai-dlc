"""Strands hooks for the Critic.

Caps ``get_artifact`` at 4 calls per invocation. The Critic needs to
read the architect's ``plan.md`` and the project's ``MEMORY.md`` /
stack profile — a small handful of reads. A higher call count
indicates a stuck loop, not legitimate re-reading.

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

GET_ARTIFACT_CAP = 4


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"get_artifact": GET_ARTIFACT_CAP}),
    ]
