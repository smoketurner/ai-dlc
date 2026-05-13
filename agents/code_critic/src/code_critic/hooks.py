"""Strands hooks for the Code-Critic.

Caps ``get_artifact`` at 2 calls per invocation. The code-critic
reads the architect's plan to ground "plan-drift" findings; a higher
count indicates a stuck loop. The op name matches the gateway-routed
``artifact_tool`` operation; critic uses the same naming axis.

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

GET_ARTIFACT_CAP = 2


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"get_artifact": GET_ARTIFACT_CAP}),
    ]
