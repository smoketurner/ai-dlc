"""Shared hook helpers used by ai-dlc agents.

:class:`ToolCallCounter`, :class:`RequirePriorCall`,
:class:`RequireAllPriorCalls`, and :class:`InputValidator` are Strands
``HookProvider`` instances. The ``strands.hooks`` import is deferred
until ``register_hooks`` is called so this module can be imported by
Strands-free code (e.g. the Implementer) without dragging Strands in.

For SDK-agnostic decision and judge-result types, plus reusable
validator functions composable with :class:`InputValidator`, see
:mod:`common.steering`.

The Strands helpers are thread-safe — Strands may invoke tools
concurrently inside one agent invocation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands.hooks import (
        BeforeInvocationEvent,
        BeforeToolCallEvent,
        HookRegistry,
    )


def effective_tool_name(tool_use: Mapping[str, Any]) -> str:
    """Resolve the operation name a hook should key on.

    AgentCore Gateway exposes one composite MCP tool per Lambda target
    (e.g. ``"artifact-tool___artifact_tool"``); the specific operation
    (``put_artifact``, ``read_memory_md``, …) rides in
    ``input["op"]``, not in ``name``. Hooks are configured with
    operation names, so they need to see the op when the call is
    gateway-routed.

    The ``"___"`` separator is the documented Gateway naming rule
    (see :data:`common.gateway_tools.ARTIFACT_TOOL`), so this covers
    every gateway target without an explicit allow-list. Local Strands
    ``@tool`` functions (e.g. ``browse_url``) have plain names and fall
    through unchanged.

    Args:
        tool_use: The ``event.tool_use`` envelope (a ``Mapping`` with at
            least ``name`` and optionally ``input``).

    Returns:
        The op name when the envelope is a gateway composite carrying a
        non-empty string ``op``; otherwise the raw ``name``.
    """
    name = str(tool_use.get("name", ""))
    if "___" not in name:
        return name
    tool_input = tool_use.get("input")
    if not isinstance(tool_input, Mapping):
        return name
    op = tool_input.get("op")
    if isinstance(op, str) and op:
        return op
    return name


class ToolCallCounter:
    """Strands hook: cap how many times a given tool may be called per invocation.

    State is reset at every ``BeforeInvocationEvent`` so the limit applies
    per-invocation, not per-process.

    Example:
        ``ToolCallCounter({"get_artifact": 3})`` denies the 4th call to
        ``get_artifact`` within one invocation.
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
        name = effective_tool_name(event.tool_use)
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
    e.g., the Retrospector must call ``read_memory_md`` before
    ``write_memory_md``.
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
        name = effective_tool_name(event.tool_use)
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


class RequireAllPriorCalls:
    """Strands hook: deny ``target`` until *every* prerequisite has been called.

    Generalisation of :class:`RequirePriorCall` for cases where multiple
    grounding reads are required before producing output — e.g. the
    Architect must call both ``read_memory_md`` and ``read_stack_profile_md``
    before ``put_artifact``.

    The reason string surfaced to the model lists *all* missing
    prerequisites, so a single retry can satisfy the gate.
    """

    def __init__(self, *, target: str, prerequisites: list[str]) -> None:
        """Build the hook.

        Args:
            target: Tool name that should be gated.
            prerequisites: Tool names that must all have been called first.
                Order is preserved when listing missing prerequisites
                back to the model.
        """
        if not prerequisites:
            msg = "RequireAllPriorCalls needs at least one prerequisite"
            raise ValueError(msg)
        self.target: str = target
        self.prerequisites: tuple[str, ...] = tuple(prerequisites)
        self.called: set[str] = set()
        self.lock: Lock = Lock()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Wire the hook into a Strands ``HookRegistry``."""
        del kwargs
        from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent  # noqa: PLC0415

        registry.add_callback(BeforeInvocationEvent, self.reset)
        registry.add_callback(BeforeToolCallEvent, self.check)

    def reset(self, event: BeforeInvocationEvent) -> None:
        """Forget which tools have been called — new invocation."""
        del event
        with self.lock:
            self.called = set()

    def check(self, event: BeforeToolCallEvent) -> None:
        """Cancel the call if any prerequisite is still outstanding."""
        name = effective_tool_name(event.tool_use)
        with self.lock:
            missing = [p for p in self.prerequisites if p not in self.called]
        if name == self.target and missing:
            joined = ", ".join(f"`{p}`" for p in missing)
            event.cancel_tool = (
                f"Cannot call `{self.target}` until every prerequisite has been "
                f"called this invocation. Still missing: {joined}. Call the "
                "missing ones first, then retry."
            )
            return
        with self.lock:
            self.called.add(name)


class InputValidator:
    """Strands hook: validate a tool call's *input* before it executes.

    Use this for content-emitting calls where the input itself needs to
    meet a structural bar — e.g. the Architect's ``put_artifact`` call
    that persists ``plan.md`` must contain every required section
    before it lands in S3.

    The validator is a pure function from the tool input dict to a list
    of human-readable problem strings. Empty list = accept; any
    contents = reject and surface them to the model so it can revise
    and re-emit. The validator should not mutate its input.

    Compose with the generic validators in :mod:`common.steering` —
    e.g. :func:`common.steering.validate_required_sections` against
    a markdown ``content`` field.
    """

    def __init__(
        self,
        *,
        tool_names: tuple[str, ...],
        validate: Callable[[dict[str, Any]], list[str]],
    ) -> None:
        """Build the validator.

        Args:
            tool_names: Tool names this validator applies to. Calls to
                tools not in this set pass through untouched.
            validate: Pure function returning a list of problem strings.
                Empty list ≡ accept.
        """
        if not tool_names:
            msg = "InputValidator needs at least one tool name"
            raise ValueError(msg)
        self.tool_names: frozenset[str] = frozenset(tool_names)
        self.validate: Callable[[dict[str, Any]], list[str]] = validate

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Wire the validator into a Strands ``HookRegistry``."""
        del kwargs
        from strands.hooks import BeforeToolCallEvent  # noqa: PLC0415

        registry.add_callback(BeforeToolCallEvent, self.check)

    def check(self, event: BeforeToolCallEvent) -> None:
        """Cancel the call when ``validate`` returns any problems."""
        name = effective_tool_name(event.tool_use)
        if name not in self.tool_names:
            return
        tool_input = event.tool_use.get("input", {})
        if not isinstance(tool_input, dict):
            return
        problems = self.validate(tool_input)
        if not problems:
            return
        joined = "; ".join(problems)
        event.cancel_tool = (
            f"`{name}` input rejected by validator: {joined}. "
            "Revise the input to address every problem above and call again."
        )
