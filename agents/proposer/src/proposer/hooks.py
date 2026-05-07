"""Strands hooks for the Proposer.

Two enforcement points:

  * Spec-dump rejection — applied via a ``model_validator`` on
    :class:`proposer.proposal.Proposal`. Strands' structured-output mode
    surfaces ``ValidationError`` to the agent, giving it a chance to
    self-correct. (No hook needed for that piece.)
  * MEMORY.md prerequisites — :class:`ProposerCallTracker` records which
    read tools have been called this invocation;
    :func:`check_memory_md_prerequisites` validates the produced
    :class:`Proposal` against that history. Edits that target
    ``docs/MEMORY.md`` require both ``read_memory_md`` and
    ``read_drift_report`` to have been called first.

The Proposer's :func:`propose` calls ``check_memory_md_prerequisites``
after the agent invocation returns; if it trips, the proposal is
rejected and the run fails. The Proposer is advisory + scheduled, so
failing once a week (or once per regression event) is acceptable — far
better than a misinformed PR landing in MEMORY.md.
"""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Any

from strands.hooks import HookCallback, HookProvider

from proposer.proposal import Proposal

if TYPE_CHECKING:
    from strands.hooks import (
        BeforeInvocationEvent,
        BeforeToolCallEvent,
        HookRegistry,
    )


class ProposerCallTracker:
    """Strands hook that records which tools the agent has called.

    Per-invocation state is reset on ``BeforeInvocationEvent`` so a long-
    running container that handles successive requests does not leak
    history between them.
    """

    def __init__(self) -> None:
        """Build a fresh tracker — empty call set."""
        self.called: set[str] = set()
        self.lock: Lock = Lock()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Wire the tracker into a Strands ``HookRegistry``."""
        del kwargs
        from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent  # noqa: PLC0415

        registry.add_callback(BeforeInvocationEvent, self.reset)
        registry.add_callback(BeforeToolCallEvent, self.track)

    def reset(self, event: BeforeInvocationEvent) -> None:
        """Forget which tools have been called — new invocation."""
        del event
        with self.lock:
            self.called = set()

    def track(self, event: BeforeToolCallEvent) -> None:
        """Record that the named tool was invoked."""
        name = str(event.tool_use["name"])
        with self.lock:
            self.called.add(name)


def build_hooks_with_tracker() -> tuple[
    list[HookProvider | HookCallback[Any]], ProposerCallTracker
]:
    """Build a fresh hooks list + the tracker the caller will validate against."""
    tracker = ProposerCallTracker()
    return [tracker], tracker


MEMORY_MD_PREREQUISITES = ("read_memory_md", "read_drift_report")


def check_memory_md_prerequisites(
    proposal: Proposal,
    tracker: ProposerCallTracker,
) -> str | None:
    """Validate that MEMORY.md edits were preceded by the right reads.

    Returns:
        ``None`` if the proposal passes; a short reason string otherwise.
    """
    targets_memory_md = any(edit.target_file == "docs/MEMORY.md" for edit in proposal.edits)
    if not targets_memory_md:
        return None
    missing = [name for name in MEMORY_MD_PREREQUISITES if name not in tracker.called]
    if not missing:
        return None
    return (
        f"proposal targets docs/MEMORY.md but did not call {missing!r} first; "
        "read those tools before proposing changes."
    )
