"""Strands Agent factory for the Proposer (research workflow).

The Proposer uses Claude Opus 4.7 — research synthesis requires reading
multiple URLs and producing a coherent comment under bounded scope.
The agent loop runs with gateway-routed grounding tools
(``artifact_tool`` ops for ``read_memory_md`` / ``read_stack_profile_md``
and ``repo_helper`` ops for reading issue threads) plus the local
``browse_url`` tool. It finishes by emitting a :class:`Proposal` via
Strands' ``structured_output_model`` parameter.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from common.gateway_tools import gateway_tools
from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from common.templating import make_template_env
from proposer.hooks import build_hooks
from proposer.proposal import Proposal
from proposer.tools import browse_url_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str, *, mcp_client: MCPClient) -> Agent:
    """Build a fresh Strands Agent for one proposer invocation.

    The caller is responsible for starting ``mcp_client`` (typically via
    ``with gateway_mcp_client() as mcp_client:``) and keeping it open
    for the lifetime of the agent call. Tool definitions from the
    gateway catalogue are spliced into the agent's tool list alongside
    the local ``browse_url`` tool.

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
            *gateway_tools(mcp_client),
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def propose_research(
    agent: Agent,
    *,
    project_slug: str,
    intent: str,
    issue_number: int,
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
        agent: A Strands ``Agent`` built by :func:`build_agent` with
            its gateway ``MCPClient`` already opened by the caller.
        project_slug: Project the research targets (drives MEMORY.md lookup).
        intent: GitHub issue body — typically contains URLs to read.
        issue_number: Issue number for the run (referenced in the prompt
            so the agent knows what's being commented on).
        target_repo: ``owner/name`` — passed to the agent so it can call
            ``repo_helper(op=list_issue_comments, ...)`` to read the
            prior thread.
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
    template = make_template_env(__package__).get_template("research_message.md.j2")
    body = template.render(
        memory_preamble=agent_memory_preamble(project_slug=project_slug, query=intent),
        project_slug=project_slug,
        issue_number=issue_number,
        target_repo=target_repo,
        intent=intent,
        triggering_comment_body=triggering_comment_body,
        triggering_commenter=triggering_commenter,
    )
    return body.rstrip("\n")
