"""Strands Agent factory for the Triage agent.

The Triage agent uses Claude Haiku 4.5 on Bedrock with a strict-JSON
output contract: the agent emits a
:class:`common.triage.TriageDecision` via Strands'
``structured_output_model`` parameter on agent invocation. The state
router branches on the resulting ``action`` to either kick off the
Architect → Critic → Implementer pipeline, hand off to the Proposer
for research, or terminate the run with a comment on the issue.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.routing import load_system_prompt, pick_variant
from common.runtime import TriageInput, default_retry_strategy, run_for_structured_output
from common.triage import TriageDecision

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-v1"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one triage invocation.

    Prompt variant is routed via :func:`common.routing.pick_variant`,
    same A/B mechanism every other Strands agent uses.
    """
    variant = pick_variant(run_id, "triage")
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=4096,
            streaming=True,
        ),
        system_prompt=load_system_prompt("triage", variant),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def triage_issue(payload: TriageInput) -> TriageDecision:
    """Run the agent against ``payload`` and return the validated decision."""
    user_message = compose_message(payload)
    agent = build_agent(payload.run_id)
    return run_for_structured_output(agent, output_model=TriageDecision, prompt=user_message)


def compose_message(payload: TriageInput) -> str:
    """Compose the user-message prompt handed to the Triage agent."""
    parts = [
        f"Issue: {payload.issue_url}",
        f"Title: {payload.issue_title}",
        f"Type: {payload.issue_type or 'unspecified'}",
        f"Labels: {', '.join(payload.issue_labels) or '(none)'}",
        "",
        "Body:",
        payload.issue_body.strip() or "(empty)",
    ]
    if payload.triggering_comment_body:
        # Distinct from ``prior_human_comments`` — that field is for the
        # awaiting-response cycle (triage previously asked, user replied).
        # This is a fresh ``@aidlc-bot <text>`` retrigger that may carry
        # new context the user wants you to consider before classifying.
        parts += [
            "",
            "User comment that retriggered this triage round:",
            payload.triggering_comment_body.strip(),
        ]
    if payload.prior_triage_count > 0:
        parts += ["", f"Prior triage rounds: {payload.prior_triage_count}"]
        if payload.prior_human_comments:
            parts += ["", "Human replies since the last triage round:"]
            for ix, comment in enumerate(payload.prior_human_comments, start=1):
                parts.append(f"---\n[{ix}] {comment.strip()}")
    return "\n".join(parts)
