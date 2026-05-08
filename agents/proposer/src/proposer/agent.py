"""Strands Agent factory for the Proposer.

The Proposer uses Claude Opus 4.7 — the proposal task requires reading
multiple summary documents and synthesising a coherent recommendation
under bounded scope. The agent loop runs with the summary-reading tools
and finishes by emitting a :class:`Proposal` via Strands'
``structured_output_model`` parameter.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from proposer.hooks import (
    ProposerCallTracker,
    build_hooks_with_tracker,
    check_memory_md_prerequisites,
)
from proposer.proposal import Proposal
from proposer.tools import (
    browse_url_tool,
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


def build_agent(run_id: str) -> tuple[Agent, ProposerCallTracker]:
    """Build a fresh Strands Agent + tracker for one proposer invocation.

    Returns the agent and the :class:`ProposerCallTracker` so the caller
    can validate the produced :class:`Proposal` against the agent's
    actual tool-call history.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "proposer")
    hooks, tracker = build_hooks_with_tracker()
    bedrock_model_id = model_id()
    agent = Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
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
            browse_url_tool,
        ],
        hooks=hooks,
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )
    return agent, tracker


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

    Raises:
        ValueError: When, after one corrective retry, the proposal still
            targets ``docs/MEMORY.md`` without first calling
            ``read_memory_md`` and ``read_drift_report``.
    """
    user_message = compose_message(
        project_slug=project_slug,
        trigger_reason=trigger_reason,
        lookback_days=lookback_days,
    )
    agent, tracker = build_agent(run_id)
    proposal = run_for_structured_output(agent, output_model=Proposal, prompt=user_message)
    violation = check_memory_md_prerequisites(proposal, tracker)
    if violation is None:
        return proposal
    retry_message = compose_retry_message(user_message, violation)
    proposal = run_for_structured_output(agent, output_model=Proposal, prompt=retry_message)
    violation = check_memory_md_prerequisites(proposal, tracker)
    if violation is not None:
        raise ValueError(violation)
    return proposal


def compose_retry_message(original_message: str, violation: str) -> str:
    """Build a corrective prompt that surfaces the prerequisite violation.

    Strands' agent loop resets per-invocation hook state on the next
    ``Agent.__call__``, so the second pass starts with an empty tracker
    and the agent must re-call the read tools before proposing edits.

    Args:
        original_message: The first-attempt prompt — replayed so the
            agent has full task context on the retry.
        violation: Reason returned by
            :func:`proposer.hooks.check_memory_md_prerequisites`.

    Returns:
        A new prompt that asks the agent to address the violation and
        retry.
    """
    return "\n".join(
        [
            original_message,
            "",
            "Your previous attempt was rejected:",
            f"  {violation}",
            "",
            "Call read_memory_md and read_drift_report this time before "
            "returning a Proposal that edits docs/MEMORY.md.",
        ]
    )


def compose_message(*, project_slug: str, trigger_reason: str, lookback_days: int) -> str:
    """Compose the user-message prompt for the proposer."""
    parts = [
        agent_memory_preamble(
            project_slug=project_slug,
            query=f"prompt and convention proposals triggered by {trigger_reason}",
        ),
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


def propose_research(*, project_slug: str, intent: str, issue_number: int, run_id: str) -> Proposal:
    """Run the agent against an issue body for the research workflow.

    The agent reads the URLs in ``intent``, synthesises findings into
    ``Proposal.summary_comment`` (which the platform posts as a comment
    on issue ``issue_number``), and may also propose edits to MEMORY.md
    or prompt files. Empty ``edits`` is fine — the comment is the
    primary deliverable.

    Args:
        project_slug: Project the research targets (drives MEMORY.md lookup).
        intent: GitHub issue body — typically contains URLs to read.
        issue_number: Issue number for the run (referenced in the prompt
            so the agent knows what's being commented on).
        run_id: Run UUID7 — drives prompt-variant selection.

    Returns:
        A validated :class:`Proposal` with ``summary_comment`` populated.
    """
    user_message = compose_research_message(
        project_slug=project_slug, intent=intent, issue_number=issue_number
    )
    agent, _tracker = build_agent(run_id)
    return run_for_structured_output(agent, output_model=Proposal, prompt=user_message)


def compose_research_message(*, project_slug: str, intent: str, issue_number: int) -> str:
    """Compose the user-message prompt for an issue-driven research run."""
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=intent),
        f"Project: {project_slug}",
        f"Source: GitHub issue #{issue_number}",
        "Trigger: research",
        "",
        "Issue body:",
        intent.strip(),
        "",
        "Steps:",
        "  1. read_memory_md to see current project conventions.",
        "  2. Identify URLs in the issue body and call browse_url on each.",
        "  3. Synthesise findings into `summary_comment` (this is posted as "
        "a comment on the issue). Aim for 8-15 short bullets an engineer can "
        "scan in 30 seconds; lead with what we should adopt, what we should "
        "avoid, and decisions worth deferring; cite the source URL on each "
        "bullet.",
        "  4. Optionally propose concrete `edits` to MEMORY.md or a prompts "
        "file when a finding warrants a change. Empty edits is fine — the "
        "comment is the primary deliverable.",
    ]
    return "\n".join(parts)
