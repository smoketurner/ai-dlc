"""Proposer-local Strands tools.

``browse_url`` (AgentCore Browser). Gateway-routed tools
(``artifact_tool`` + ``repo_helper``) are spliced in by
:func:`proposer.agent.build_agent`.
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)
