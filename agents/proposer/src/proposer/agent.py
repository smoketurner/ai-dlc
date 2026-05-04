"""Strands Agent factory for the Proposer.

The Proposer uses Claude Opus 4.7 — the proposal task requires reading
multiple summary documents and synthesising a coherent recommendation
under bounded scope. Output is constrained to a :class:`Proposal` JSON
shape via Strands' ``structured_output``.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.routing import load_system_prompt, pick_variant
from proposer.proposal import Proposal
from proposer.tools import (
    read_drift_report_tool,
    read_eval_aggregate_tool,
    read_few_shot_summary_tool,
    read_memory_md_tool,
    read_rejection_summary_tool,
)

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one proposer invocation.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "proposer")
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            region_name=os.environ["AWS_REGION"],
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("proposer", variant),
        tools=[
            read_eval_aggregate_tool,
            read_drift_report_tool,
            read_rejection_summary_tool,
            read_few_shot_summary_tool,
            read_memory_md_tool,
        ],
    )


def propose(*, project_slug: str, trigger_reason: str, lookback_days: int, run_id: str) -> Proposal:
    """Run the agent and return the validated Proposal.

    Args:
        project_slug: Project the proposal targets (used to read MEMORY.md).
        trigger_reason: Why the proposer is running (``"scheduled"`` or
            ``"regression"``). Surfaced to the agent so it can adjust
            sensitivity.
        lookback_days: Window the agent should consider for signals.
        run_id: Run UUID7 — drives prompt-variant selection.

    Returns:
        A validated :class:`Proposal`. May contain zero edits if the
        agent decides no action is warranted.
    """
    user_message = compose_message(
        project_slug=project_slug,
        trigger_reason=trigger_reason,
        lookback_days=lookback_days,
    )
    agent = build_agent(run_id)
    return agent.structured_output(Proposal, user_message)


def compose_message(*, project_slug: str, trigger_reason: str, lookback_days: int) -> str:
    """Compose the user-message prompt for the proposer."""
    parts = [
        f"Project: {project_slug}",
        f"Trigger: {trigger_reason}",
        f"Lookback window: {lookback_days} days",
        "",
        "Steps:",
        "  1. read_memory_md to see the current project conventions.",
        "  2. read_eval_aggregate, read_drift_report to see pass-rate trends.",
        "  3. read_rejection_summary for category distribution from telemetry.",
        "  4. read_few_shot_summary for the size of the curated example bank.",
        "  5. Decide whether the signals warrant a proposal. Return Proposal "
        "JSON — empty `edits` if not.",
    ]
    return "\n".join(parts)
