"""Strands hooks for the Architect.

Three rules, all firing on ``BeforeToolCallEvent``:

1. The agent must call both ``read_memory_md`` *and* ``read_stack_profile_md``
   before ``put_artifact`` — without either, the plan would be ungrounded
   in project conventions or stack reality.
2. The body passed to ``put_artifact`` (which becomes ``plan.md`` in S3)
   must contain every canonical section heading from
   :data:`architect.plan.SECTION_HEADINGS`. The validator only fires when
   ``key`` ends with ``plan.md`` so other artifact types pass through.

Both rules cancel the call with an actionable message; Strands surfaces
that to the model so it can grounding-read or revise the body and try
again on the same turn.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from architect.plan import SECTION_HEADINGS
from common.hooks import InputValidator, RequireAllPriorCalls
from common.steering import validate_required_sections

PLAN_KEY_SUFFIX = "plan.md"
PLAN_HEADING_LEVEL = 2
PLAN_SECTION_NAMES: list[str] = [h.removeprefix("##").strip() for h in SECTION_HEADINGS]


def validate_plan_artifact(tool_input: dict[str, Any]) -> list[str]:
    """Return missing-section problems for ``put_artifact`` calls writing ``plan.md``.

    Returns an empty list (= accept) for any other artifact key, so this
    validator can safely cover the entire ``put_artifact`` tool surface.

    Args:
        tool_input: ``put_artifact`` input — expected keys are ``op``,
            ``key``, and ``content``.

    Returns:
        Problem strings to surface back to the model. Empty = accept.
    """
    key = str(tool_input.get("key", ""))
    if not key.endswith(PLAN_KEY_SUFFIX):
        return []
    content = str(tool_input.get("content", ""))
    missing = validate_required_sections(
        content,
        PLAN_SECTION_NAMES,
        heading_level=PLAN_HEADING_LEVEL,
    )
    if not missing:
        return []
    joined = ", ".join(missing)
    return [f"plan.md is missing required level-2 section(s): {joined}"]


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation.

    Hooks carry per-invocation state, so each :class:`strands.Agent` gets
    its own instances rather than sharing one across runs.
    """
    return [
        RequireAllPriorCalls(
            target="put_artifact",
            prerequisites=["read_memory_md", "read_stack_profile_md"],
        ),
        InputValidator(
            tool_names=("put_artifact",),
            validate=validate_plan_artifact,
        ),
    ]
