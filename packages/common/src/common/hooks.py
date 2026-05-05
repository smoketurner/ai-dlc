"""Shared hook helpers used by ai-dlc agents.

Two flavors live side by side:

  - :func:`validate_no_spec_dump` is pure Python — used by both Strands
    agents (in their ``hooks.py``) and the Implementer (Claude Agent SDK
    ``PostToolUse`` validator).
  - :class:`ToolCallCounter` and :class:`RequirePriorCall` are Strands
    ``HookProvider`` instances. The ``strands.hooks`` import is deferred
    until ``register_hooks`` is called so this module can be imported by
    Strands-free code (e.g. the Implementer) without dragging Strands in.

The Strands helpers are thread-safe — Strands may invoke tools
concurrently inside one agent invocation.
"""

from __future__ import annotations

import re
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands.hooks import (
        BeforeInvocationEvent,
        BeforeToolCallEvent,
        HookRegistry,
    )


SPEC_LEAK_PATTERN = re.compile(r"(?im)^\s{0,3}#{1,6}\s+(requirements|design|tasks)(?:\.md)?\s*$")


def validate_no_spec_dump(text: str) -> str | None:
    """Detect raw spec-document headings leaking into agent output.

    Catches the obvious failure mode where an agent quotes the spec
    document headers verbatim into a PR body, summary, or critique.

    Args:
        text: Candidate output — PR body, summary, etc.

    Returns:
        A short reason string when a leak is detected; ``None`` otherwise.
    """
    match = SPEC_LEAK_PATTERN.search(text)
    if match is None:
        return None
    return f"detected spec heading leak: {match.group(0).strip()!r}"


class ToolCallCounter:
    """Strands hook: cap how many times a given tool may be called per invocation.

    State is reset at every ``BeforeInvocationEvent`` so the limit applies
    per-invocation, not per-process.

    Example:
        ``ToolCallCounter({"read_spec_doc": 3})`` denies the 4th call to
        ``read_spec_doc`` within one invocation.
    """

    def __init__(self, limits: dict[str, int]) -> None:
        """Build the counter.

        Args:
            limits: Map of tool name → maximum calls per invocation. Tools
                not listed are unbounded.
        """
        self.limits: dict[str, int] = dict(limits)
        self.counts: dict[str, int] = {}
        self.lock: Lock = Lock()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Wire the counter into a Strands ``HookRegistry``."""
        del kwargs
        # Deferred so `common.hooks` is importable from Implementer code, which
        # does not depend on Strands. Strands agents that call this method
        # already have strands installed.
        from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent  # noqa: PLC0415

        registry.add_callback(BeforeInvocationEvent, self.reset)
        registry.add_callback(BeforeToolCallEvent, self.check)

    def reset(self, event: BeforeInvocationEvent) -> None:
        """Reset per-invocation state."""
        del event
        with self.lock:
            self.counts = {}

    def check(self, event: BeforeToolCallEvent) -> None:
        """Increment the count and cancel the call if the cap is exceeded."""
        name = str(event.tool_use["name"])
        limit = self.limits.get(name)
        if limit is None:
            return
        with self.lock:
            count = self.counts.get(name, 0) + 1
            self.counts[name] = count
        if count > limit:
            event.cancel_tool = (
                f"Tool `{name}` has reached its per-invocation cap of {limit}. "
                "Use the result you already have; do not call it again."
            )


class RequirePriorCall:
    """Strands hook: deny ``target`` until ``prerequisite`` has been called.

    Useful when an agent must read context before producing output —
    e.g., the Architect must call ``read_memory_md`` before
    ``write_spec_doc``.
    """

    def __init__(self, *, target: str, prerequisite: str) -> None:
        """Build the hook.

        Args:
            target: Tool name that should be gated.
            prerequisite: Tool name that must have been called first.
        """
        self.target: str = target
        self.prerequisite: str = prerequisite
        self.called: set[str] = set()
        self.lock: Lock = Lock()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Wire the hook into a Strands ``HookRegistry``."""
        del kwargs
        # Deferred so `common.hooks` is importable from Implementer code, which
        # does not depend on Strands. Strands agents that call this method
        # already have strands installed.
        from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent  # noqa: PLC0415

        registry.add_callback(BeforeInvocationEvent, self.reset)
        registry.add_callback(BeforeToolCallEvent, self.check)

    def reset(self, event: BeforeInvocationEvent) -> None:
        """Forget which tools have been called — new invocation."""
        del event
        with self.lock:
            self.called = set()

    def check(self, event: BeforeToolCallEvent) -> None:
        """Cancel the call if ``target`` runs before ``prerequisite``."""
        name = str(event.tool_use["name"])
        with self.lock:
            already_called = self.prerequisite in self.called
        if name == self.target and not already_called:
            event.cancel_tool = (
                f"Cannot call `{self.target}` before `{self.prerequisite}` "
                f"has been called this invocation. Call `{self.prerequisite}` "
                "first, then retry."
            )
            return
        with self.lock:
            self.called.add(name)
