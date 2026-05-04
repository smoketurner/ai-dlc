"""A/B prompt-variant routing for the agent fleet.

Each agent has a default ``prompts.py``. When humans want to A/B-test a
prompt rewrite, they (or the Proposer agent's PR) add a ``prompts_b.py``
alongside it. The agent's ``build_agent`` calls :func:`pick_variant` with
the run id; the deterministic hash decides whether *this* run gets the A
or the B variant.

The choice is stable for the run: every per-task invocation of the same
agent inside one run picks the same variant, so a multi-task run doesn't
end up with a half-A / half-B mix. Variant tagging flows through the
``actor_id`` field on every event the agent emits, so the eval matrix
(Phase 9b) can compare A vs B outcomes.

When ``prompts_b`` doesn't exist for a given agent, :func:`load_system_prompt`
silently falls back to the A module — so deploying B is a single PR
adding the file, no plumbing changes needed.
"""

from __future__ import annotations

import hashlib
import importlib
from typing import Literal

Variant = Literal["a", "b"]


def pick_variant(run_id: str, agent_name: str) -> Variant:
    """Deterministically pick a prompt variant for ``(run_id, agent_name)``.

    Stable for the run + agent: a single run always sees the same variant
    for a given agent. Different agents in the same run can see different
    variants (so we can A/B one agent without blowing away the others).

    Args:
        run_id: The run UUID7 string.
        agent_name: The agent's package name — e.g., ``"architect"``.

    Returns:
        ``"a"`` or ``"b"``.
    """
    digest = hashlib.sha256(f"{run_id}:{agent_name}".encode()).digest()
    return "b" if digest[0] & 1 else "a"


def variant_actor_id(agent_name: str, variant: Variant) -> str:
    """Build the actor_id telemetry tag for ``(agent_name, variant)``.

    Used as the ``actor_id`` field on event envelopes the agent emits so
    downstream consumers (event_projector, telemetry, eval_runner) can
    split metrics by variant.
    """
    return f"{agent_name}-{variant}"


def load_system_prompt(agent_name: str, variant: Variant) -> str:
    """Resolve the SYSTEM_PROMPT for ``(agent_name, variant)``.

    Imports ``{agent_name}.prompts_b`` when ``variant=="b"`` *and* the
    module exists; otherwise falls back to ``{agent_name}.prompts``.
    """
    if variant == "b":
        try:
            mod_b = importlib.import_module(f"{agent_name}.prompts_b")
        except ModuleNotFoundError:
            mod_b = None
        if mod_b is not None:
            prompt = getattr(mod_b, "SYSTEM_PROMPT", None)
            if isinstance(prompt, str):
                return prompt
    mod = importlib.import_module(f"{agent_name}.prompts")
    return mod.SYSTEM_PROMPT
