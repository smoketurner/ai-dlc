"""Tests for the Proposer corrective-retry path.

Covers ``compose_retry_message`` (prompt shape) and ``propose`` (one-shot
retry on prerequisite violation, raises only when the second attempt
also violates).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from proposer.agent import compose_retry_message, propose
from proposer.hooks import ProposerCallTracker
from proposer.proposal import FileEdit, Proposal


def make_memory_md_proposal() -> Proposal:
    return Proposal(
        rationale="Trends justify a structlog convention update.",
        supporting_evidence=["evals/results lookback=30d"],
        edits=[
            FileEdit(
                target_file="docs/MEMORY.md",
                proposed_content="# Conventions\n- Use structlog.",
            )
        ],
        pr_title="proposer: document structlog convention",
        pr_body="The few-shot bank shows structlog use across many runs.",
    )


def make_no_op_proposal() -> Proposal:
    return Proposal(rationale="No edits this cycle.")


def test_compose_retry_message_includes_violation_and_directive() -> None:
    out = compose_retry_message(
        original_message="Original task prompt",
        violation="proposal targets docs/MEMORY.md but did not call ['read_memory_md'] first",
    )
    assert "Original task prompt" in out
    assert "Your previous attempt was rejected" in out
    assert "['read_memory_md']" in out
    assert "Call read_memory_md and read_drift_report" in out


def test_propose_returns_proposal_when_first_attempt_passes() -> None:
    """No retry happens when prerequisites are satisfied on the first call."""
    tracker = ProposerCallTracker()
    tracker.called = {"read_memory_md", "read_drift_report"}
    proposal = make_memory_md_proposal()
    with (
        patch("proposer.agent.build_agent", return_value=(_AgentStub(), tracker)),
        patch(
            "proposer.agent.run_for_structured_output",
            return_value=proposal,
        ) as mocked,
    ):
        result = propose(
            project_slug="sample",
            trigger_reason="scheduled",
            lookback_days=30,
            run_id="01HV0000000000000000000001",
        )
    assert result is proposal
    assert mocked.call_count == 1


def test_propose_retries_once_when_prerequisites_missing_then_succeeds() -> None:
    """First attempt violates; second attempt has read tools; final result returned."""
    tracker = ProposerCallTracker()
    first = make_memory_md_proposal()
    second = make_memory_md_proposal()
    call_count = [0]

    def fake_run(agent: Any, *, output_model: Any, prompt: str) -> Proposal:
        del agent, output_model, prompt
        call_count[0] += 1
        if call_count[0] == 1:
            return first
        tracker.called = {"read_memory_md", "read_drift_report"}
        return second

    with (
        patch("proposer.agent.build_agent", return_value=(_AgentStub(), tracker)),
        patch("proposer.agent.run_for_structured_output", side_effect=fake_run),
    ):
        result = propose(
            project_slug="sample",
            trigger_reason="scheduled",
            lookback_days=30,
            run_id="01HV0000000000000000000002",
        )
    assert result is second
    assert call_count[0] == 2


def test_propose_raises_when_retry_also_violates() -> None:
    """Two consecutive violations exhaust the budget — fail loudly."""
    tracker = ProposerCallTracker()
    proposal = make_memory_md_proposal()
    with (
        patch("proposer.agent.build_agent", return_value=(_AgentStub(), tracker)),
        patch("proposer.agent.run_for_structured_output", return_value=proposal),
        pytest.raises(ValueError, match=r"docs/MEMORY\.md"),
    ):
        propose(
            project_slug="sample",
            trigger_reason="scheduled",
            lookback_days=30,
            run_id="01HV0000000000000000000003",
        )


def test_propose_no_retry_when_proposal_does_not_target_memory_md() -> None:
    """Empty-edits proposals never trip the prerequisite check."""
    tracker = ProposerCallTracker()
    proposal = make_no_op_proposal()
    with (
        patch("proposer.agent.build_agent", return_value=(_AgentStub(), tracker)),
        patch(
            "proposer.agent.run_for_structured_output",
            return_value=proposal,
        ) as mocked,
    ):
        result = propose(
            project_slug="sample",
            trigger_reason="scheduled",
            lookback_days=30,
            run_id="01HV0000000000000000000004",
        )
    assert result is proposal
    assert mocked.call_count == 1


class _AgentStub:
    """Stand-in for a Strands ``Agent`` — never invoked directly in these tests."""
