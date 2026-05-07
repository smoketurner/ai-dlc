"""Strands Agent factory for the Triage agent.

The Triage agent uses Claude Haiku 4.5 on Bedrock with a strict-JSON
output contract: the agent emits a
:class:`common.triage.TriageDecision` via Strands'
``structured_output_model`` parameter on agent invocation. The Step
Functions ``Choice`` state branches on the resulting ``action`` and
``workflow_kind``.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.routing import load_system_prompt, pick_variant
from common.runtime import TriageInput, run_for_structured_output
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
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=4096,
            streaming=True,
        ),
        system_prompt=load_system_prompt("triage", variant),
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
    if payload.prior_triage_count > 0:
        parts += ["", f"Prior triage rounds: {payload.prior_triage_count}"]
        if payload.prior_human_comments:
            parts += ["", "Human replies since the last triage round:"]
            for ix, comment in enumerate(payload.prior_human_comments, start=1):
                parts.append(f"---\n[{ix}] {comment.strip()}")
    return "\n".join(parts)
