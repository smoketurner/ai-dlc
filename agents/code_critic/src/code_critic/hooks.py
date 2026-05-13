"""Strands hooks for the Code-Critic.

Caps ``read_plan_doc`` at 2 calls per invocation. The code-critic
reads the architect's plan to ground "plan-drift" findings; a higher
count indicates a stuck loop.

The "Code-Critic must find at least one issue" rule lives on
:class:`code_critic.critique.Critique` itself as a ``min_length=1``
constraint on ``issues``. Strands' structured-output mode surfaces the
resulting Pydantic ``ValidationError`` to the agent, giving it a chance
to self-correct before the run completes.
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
