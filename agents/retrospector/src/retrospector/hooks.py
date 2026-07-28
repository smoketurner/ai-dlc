"""Strands hooks for the Retrospector.

Two rules guard the lesson-extraction pass:

1. ``write_memory_md`` is gated on a prior ``read_memory_md`` — the
   retrospector must see the current ``MEMORY.md`` before proposing
   updates to it. Otherwise the agent can wholesale-overwrite an
   established index it has never inspected.
2. ``get_artifact`` is capped per invocation as a stuck-loop backstop,
   not as a budget — see :data:`GET_ARTIFACT_CAP`.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import RequirePriorCall, ToolCallCounter

GET_ARTIFACT_CAP = 64
"""Ceiling on ``get_artifact`` calls in one invocation.

Capture mode hands the agent one validator artifact key per validator
per revision round — ``3 * (revision_count + 1)``, so 12 keys on a
revision-cap failure and up to 51 at the ``revision_count`` ceiling —
and the capture template tells it to read every one. Anything lower
truncates exactly the multi-round history the retrospector exists to
find patterns in. Matches the ``max_length=64`` bound on
``RetrospectorInput.validation_artifact_keys``.
"""


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation."""
    return [
        ToolCallCounter({"get_artifact": GET_ARTIFACT_CAP}),
        RequirePriorCall(target="write_memory_md", prerequisite="read_memory_md"),
    ]
