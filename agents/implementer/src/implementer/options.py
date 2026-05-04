"""Builds the ``ClaudeAgentOptions`` for one Implementer invocation."""

from __future__ import annotations

import os

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from common.routing import load_system_prompt, pick_variant
from implementer.hooks import deny_dangerous_bash, deny_sensitive_writes

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_BUDGET_USD = 5.0
DEFAULT_MAX_TURNS = 50


def model_id() -> str:
    """Claude model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def working_dir() -> str:
    """Implementer's per-session checkout root inside the container."""
    return os.environ.get("AIDLC_WORKSPACE", "/workspace/repo")


def build_options(run_id: str) -> ClaudeAgentOptions:
    """Build the ClaudeAgentOptions used for one task invocation.

    System prompt is selected via A/B routing — half of runs (deterministic
    in ``run_id``) use ``implementer.prompts_b`` if present.
    """
    variant = pick_variant(run_id, "implementer")
    return ClaudeAgentOptions(
        model=model_id(),
        system_prompt=load_system_prompt("implementer", variant),
        cwd=working_dir(),
        permission_mode="acceptEdits",
        max_turns=DEFAULT_MAX_TURNS,
        max_budget_usd=DEFAULT_BUDGET_USD,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[deny_dangerous_bash]),
                HookMatcher(matcher="Write|Edit", hooks=[deny_sensitive_writes]),
            ],
        },
        env={
            # Claude Code uses Bedrock when this is set.
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        },
    )
