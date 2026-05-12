"""Environment-variable accessors and tunable constants.

Single source of truth for everything the router reads out of the
process environment, plus the small handful of magic numbers that
gate dispatch behaviour. Lives at the bottom of the dependency DAG
— no internal imports.
"""

from __future__ import annotations

import os

DISPATCH_READ_TIMEOUT_SECONDS = 10.0
DISPATCH_CONNECT_TIMEOUT_SECONDS = 10.0

MAX_DISPATCH_FAILURES = 3
"""Open the per-row circuit breaker after this many consecutive
``rollback_after_failure`` cycles. Each rollback increments
``dispatch_failure_count`` on the target row; a successful agent
completion (``*.READY`` event) resets it to 0 in the projector.
"""


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def artifacts_bucket() -> str:
    """Artifacts S3 bucket — for synthetic spec uploads."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def runtime_arn(name: str) -> str:
    """Read the runtime ARN env var for a named agent.

    All seven agent runtimes are passed in via env vars in the form
    ``AIDLC_{NAME}_RUNTIME_ARN``. Missing means the runtime hasn't been
    provisioned yet (bootstrap apply); the dispatch handler returns a
    Noop instead of dispatching, and the run sits until the next deploy
    completes the runtime ARNs.
    """
    return os.environ.get(f"AIDLC_{name.upper()}_RUNTIME_ARN", "")


def repo_helper_function_name() -> str:
    """Lambda function name for ``repo_helper`` invocations."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME", "")


def lint_gate_function_name() -> str:
    """Lambda function name for ``lint_gate`` invocations."""
    return os.environ.get("AIDLC_LINT_GATE_FUNCTION_NAME", "")


def github_bot_login() -> str:
    """GitHub login of the App's bot user (e.g., ``ai-dlc[bot]``).

    Used by :func:`common.github_mentions.strip_control_prefixes` when
    the router prepares the triage / architect payload — a leading
    ``@<bot_login>`` on the triggering comment is noise to the LLM.
    Empty string when the env var isn't wired (the strip helper falls
    back to slash-command-only stripping).
    """
    return os.environ.get("AIDLC_GITHUB_BOT_LOGIN", "")
