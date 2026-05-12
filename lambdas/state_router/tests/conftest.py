"""Shared fixtures for state_router tests.

The dispatch handlers read agent runtime ARNs from env vars; tests
populate them with sentinel values via this fixture so handler logic
exercises the full dispatch path. Tests that want to assert on
"runtime not yet provisioned" branches override the env in their own
``monkeypatch`` block.
"""

from __future__ import annotations

import pytest

AGENTS = (
    "architect",
    "code_critic",
    "critic",
    "implementer",
    "proposer",
    "reviewer",
    "tester",
    "triage",
)


@pytest.fixture(autouse=True)
def runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate every ``AIDLC_<AGENT>_RUNTIME_ARN`` and friends."""
    for agent in AGENTS:
        monkeypatch.setenv(
            f"AIDLC_{agent.upper()}_RUNTIME_ARN",
            f"arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/{agent}",
        )
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", "ai-dlc-repo-helper")
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", "ai-dlc-artifacts")
    monkeypatch.setenv("AIDLC_RUNS_TABLE", "ai-dlc-runs")
