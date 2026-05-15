"""Environment-variable accessors for the state router.

Lives at the bottom of the dependency DAG — no internal imports.
"""

from __future__ import annotations

import os

DISPATCH_READ_TIMEOUT_SECONDS = 10.0
DISPATCH_CONNECT_TIMEOUT_SECONDS = 10.0


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def runtime_arn(name: str) -> str:
    """Read the runtime ARN env var for a named agent.

    Env-var convention is ``AIDLC_{NAME}_RUNTIME_ARN``. Missing means
    the runtime hasn't been provisioned yet — the executor logs and
    emits ``RUN.FAILED`` rather than wedging on an empty string.
    """
    return os.environ.get(f"AIDLC_{name.upper()}_RUNTIME_ARN", "")


def github_bot_login() -> str:
    """GitHub login of the App's bot user (e.g., ``aidlc-bot``).

    Used by :func:`common.github_mentions.strip_bot_mention` when the
    router prepares the triage / architect payload — a leading
    ``@<bot_login>`` on the triggering comment is noise to the LLM.
    Empty when unset (the strip helper falls back to slash-command-only
    stripping).
    """
    return os.environ.get("AIDLC_GITHUB_BOT_LOGIN", "")
