"""Strands hooks for the Critic.

Caps ``read_spec_doc`` at 3 calls per invocation (one per spec document).
A 4th call indicates a stuck loop, not legitimate re-reading.

Note on the deferred severity-bucket completeness check
-------------------------------------------------------
The original plan called for an ``AfterInvocationEvent`` that re-prompts
when the produced :class:`critic.critique.Critique` is missing severity
buckets. Two technical realities pushed that out of scope:

  * Strands' ``Agent.structured_output`` returns the parsed object as a
    return value; ``AfterInvocationEvent.result`` is ``None`` for those
    calls (per the Strands 1.38 docstring on ``AfterInvocationEvent``),
    so a hook can't read the validated :class:`Critique`.
  * Forcing every severity bucket on every critique is the wrong rule:
    a strong spec may legitimately produce only ``low``-severity issues.

If we still want this, the right place is a ``model_validator`` on
:class:`Critique` that rejects an entirely-empty ``issues`` list (i.e.
"the Critic must find at least one thing"). That's a contract change
better landed as its own PR.
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
