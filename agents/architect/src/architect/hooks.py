"""Strands hooks for the Architect.

Enforces a single rule: the agent must call ``read_memory_md`` before
``write_spec_doc``. Without this, the agent could draft a spec without
having seen the project's MEMORY.md conventions — exactly the failure
mode that drove issue #18.
"""

from __future__ import annotations

from typing import Any

from strands.hooks import HookCallback, HookProvider

from common.hooks import RequirePriorCall


def build_hooks() -> list[HookProvider | HookCallback[Any]]:
    """Build a fresh list of hook providers for one agent invocation.

    Hooks carry per-invocation state, so each :class:`strands.Agent` gets
    its own instances rather than sharing one across runs.
    """
    return [
        RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md"),
    ]
