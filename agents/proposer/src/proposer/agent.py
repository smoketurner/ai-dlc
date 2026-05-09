"""Strands Agent factory for the Proposer (research workflow).

The Proposer uses Claude Opus 4.7 — research synthesis requires reading
multiple URLs and producing a coherent comment under bounded scope. The
agent loop runs with the read/browse tools and finishes by emitting a
:class:`Proposal` via Strands' ``structured_output_model`` parameter.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from proposer.proposal import Proposal
from proposer.tools import (
    browse_url_tool,
    list_issue_comments_tool,
    read_memory_md_tool,
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
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("proposer", variant),
        tools=[
            read_memory_md_tool,
            browse_url_tool,
            list_issue_comments_tool,
        ],
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def propose_research(
    *,
    project_slug: str,
    intent: str,
    issue_number: int,
    run_id: str,
    target_repo: str | None = None,
    triggering_comment_body: str = "",
    triggering_commenter: str = "",
) -> Proposal:
    """Run the agent against an issue body for the research workflow.

    The agent reads the URLs in ``intent``, synthesises findings into
    ``Proposal.summary_comment`` (which the platform posts as a comment
    on issue ``issue_number``), and may also propose edits to MEMORY.md
    or prompt files. Empty ``edits`` is fine — the comment is the
    primary deliverable.

    When ``triggering_comment_body`` is set, the run was minted from a
    follow-up ``@aidlc-bot`` comment on the issue. The agent reads the
    human's free-form ask and may emit ``proposed_issues`` to spawn
    scoped follow-up issues when explicitly requested.

    Args:
        project_slug: Project the research targets (drives MEMORY.md lookup).
        intent: GitHub issue body — typically contains URLs to read.
        issue_number: Issue number for the run (referenced in the prompt
            so the agent knows what's being commented on).
        run_id: Run UUID7 — drives prompt-variant selection.
        target_repo: ``owner/name`` — passed to the agent so it can call
            ``list_issue_comments`` to read the prior thread.
        triggering_comment_body: Body of the comment that minted this
            run (empty for runs minted from initial issue assignment).
        triggering_commenter: GitHub login of the user who posted the
            triggering comment (empty when there is no triggering comment).

    Returns:
        A validated :class:`Proposal` with ``summary_comment`` populated.
    """
    user_message = compose_research_message(
        project_slug=project_slug,
        intent=intent,
        issue_number=issue_number,
        target_repo=target_repo,
        triggering_comment_body=triggering_comment_body,
        triggering_commenter=triggering_commenter,
    )
    agent = build_agent(run_id)
    return run_for_structured_output(agent, output_model=Proposal, prompt=user_message)


def compose_research_message(
    *,
    project_slug: str,
    intent: str,
    issue_number: int,
    target_repo: str | None = None,
    triggering_comment_body: str = "",
    triggering_commenter: str = "",
) -> str:
    """Compose the user-message prompt for an issue-driven research run."""
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=intent),
        f"Project: {project_slug}",
        f"Source: GitHub issue #{issue_number}",
    ]
    if target_repo:
        parts.append(f"Target repo: {target_repo}")
    parts.extend(["Trigger: research", "", "Issue body:", intent.strip()])
    if triggering_comment_body:
        attribution = f" by @{triggering_commenter}" if triggering_commenter else ""
        parts.extend(
            [
                "",
                f"Follow-up comment{attribution}:",
                triggering_comment_body.strip(),
                "",
                "This run was triggered by the follow-up comment above. "
                "Read the prior thread on this issue with list_issue_comments "
                "before deciding what to do — your earlier synthesis comment "
                "is the source the user is referring to.",
                "",
                "If the user explicitly asks you to create / spawn / file "
                "GitHub issues for follow-up work, populate `proposed_issues` "
                "with one entry per issue (title in short imperative form, "
                "body with scope + acceptance criteria, labels including "
                "`aidlc-spawned`). If the user asks for prioritization or "
                "summary without asking for issues, leave `proposed_issues` "
                "empty and use `summary_comment` to reply.",
            ]
        )
    parts.extend(
        [
            "",
            "Steps:",
            "  1. read_memory_md to see current project conventions.",
            "  2. Identify URLs in the issue body and call browse_url on each. "
            "Skip this when the follow-up comment is asking about prior "
            "synthesis (call list_issue_comments instead).",
            "  3. Synthesise findings into `summary_comment` (this is posted "
            "as a comment on the issue). Aim for 8-15 short bullets an "
            "engineer can scan in 30 seconds; lead with what we should adopt, "
            "what we should avoid, and decisions worth deferring; cite the "
            "source URL on each bullet.",
            "  4. Optionally propose concrete `edits` to MEMORY.md or a "
            "prompts file when a finding warrants a change. Empty edits is "
            "fine — the comment is the primary deliverable.",
            "  5. Populate `proposed_issues` only when the human explicitly "
            "asked for issue creation in their follow-up comment. Empty "
            "`proposed_issues` is the default.",
        ],
    )
    return "\n".join(parts)
