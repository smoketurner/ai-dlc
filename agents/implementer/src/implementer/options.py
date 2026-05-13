"""Builds the ``ClaudeAgentOptions`` for one Implementer invocation."""

from __future__ import annotations

import os

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from common.gateway_tools import fetch_gateway_token, gateway_url
from common.routing import load_system_prompt, pick_variant
from implementer.finish import FINISH_SERVER_NAME, FINISH_TOOL_NAME, FinishSink, build_finish_server
from implementer.hooks import (
    audit_log_writes,
    deny_dangerous_bash,
    deny_sensitive_writes,
    validate_finish_report,
)
from implementer.prompts import RESOLVER_SYSTEM_PROMPT

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
FALLBACK_MODEL_ID = "us.anthropic.claude-haiku-4-5"
DEFAULT_BUDGET_USD = 5.0
DEFAULT_MAX_TURNS = 50
RESOLVER_BUDGET_USD = 1.0
RESOLVER_MAX_TURNS = 12

GATEWAY_SERVER_NAME = "gateway"


def model_id() -> str:
    """Claude model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def working_dir() -> str:
    """Implementer's per-session checkout root inside the container."""
    return os.environ.get("AIDLC_WORKSPACE", "/workspace/repo")


def build_options(run_id: str, *, finish_sink: FinishSink) -> ClaudeAgentOptions:
    """Build the ClaudeAgentOptions used for one task invocation.

    The ``finish_sink`` is bound into the ``finish`` MCP tool so the agent's
    structured report can be read back after the SDK loop drains. A
    ``PostToolUse`` matcher on ``finish`` re-validates the payload and
    rejects spec dumps so Claude retries.

    The per-agent AgentCore Gateway is wired in as an HTTP MCP server so
    Claude can call ``mcp__gateway__artifact_tool`` (for any
    ``put_artifact`` / ``get_artifact`` / ``read_memory_md`` /
    ``read_stack_profile_md`` / ``write_memory_md``) and
    ``mcp__gateway__repo_helper`` (for any
    ``comment_pr`` / ``comment_issue`` / ``list_pr_comments`` /
    ``list_issue_comments`` / ``list_check_runs`` / ``create_issue`` etc.)
    mid-loop. The JWT is minted at build-time via AgentCore Identity —
    M2M tokens last ~1h, well beyond a bounded implementer session.

    System prompt is selected via A/B routing — half of runs (deterministic
    in ``run_id``) use ``implementer.prompts_b`` if present.
    """
    variant = pick_variant(run_id, "implementer")
    finish_server = build_finish_server(finish_sink)
    return ClaudeAgentOptions(
        model=model_id(),
        fallback_model=FALLBACK_MODEL_ID,
        system_prompt=load_system_prompt("implementer", variant),
        cwd=working_dir(),
        permission_mode="acceptEdits",
        max_turns=DEFAULT_MAX_TURNS,
        max_budget_usd=DEFAULT_BUDGET_USD,
        thinking={"type": "adaptive"},
        effort="high",
        allowed_tools=[
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "Bash",
            "Monitor",
            "WebFetch",
            "WebSearch",
            "TaskCreate",
            "TaskGet",
            "TaskList",
            "TaskUpdate",
            "TaskStop",
            "TaskOutput",
            "TodoWrite",
            "EnterWorktree",
            "ExitWorktree",
            "ListMcpResourcesTool",
            "ReadMcpResourceTool",
            "ToolSearch",
            "Skill",
            FINISH_TOOL_NAME,
            f"mcp__{GATEWAY_SERVER_NAME}__artifact_tool",
            f"mcp__{GATEWAY_SERVER_NAME}__repo_helper",
        ],
        mcp_servers={
            FINISH_SERVER_NAME: finish_server,
            GATEWAY_SERVER_NAME: {
                "type": "http",
                "url": gateway_url(),
                "headers": {"Authorization": f"Bearer {fetch_gateway_token()}"},
            },
        },
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[deny_dangerous_bash]),
                HookMatcher(matcher="Write|Edit", hooks=[deny_sensitive_writes]),
            ],
            "PostToolUse": [
                HookMatcher(matcher=FINISH_TOOL_NAME, hooks=[validate_finish_report]),
                HookMatcher(matcher="Write|Edit|Bash|NotebookEdit", hooks=[audit_log_writes]),
            ],
        },
        env={
            # Claude Code uses Bedrock when this is set.
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        },
    )


def build_resolver_options() -> ClaudeAgentOptions:
    """Tight ClaudeAgentOptions for the merge-conflict resolver sub-session.

    The resolver edits files only — no Bash, no Write, no MCP servers,
    no finish tool. The wrapper detects completion by checking the
    working tree for remaining conflict markers after the session ends.
    Budget and turn cap are aggressive so a stuck resolver can't burn
    much before the wrapper aborts the merge.
    """
    return ClaudeAgentOptions(
        model=model_id(),
        fallback_model=FALLBACK_MODEL_ID,
        system_prompt=RESOLVER_SYSTEM_PROMPT,
        cwd=working_dir(),
        permission_mode="acceptEdits",
        max_turns=RESOLVER_MAX_TURNS,
        max_budget_usd=RESOLVER_BUDGET_USD,
        allowed_tools=["Read", "Edit"],
        env={
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        },
    )
