"""Tests for ``proposer.hooks`` and the spec-dump model_validator on ``Proposal``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from proposer.hooks import (
    MEMORY_MD_PREREQUISITES,
    ProposerCallTracker,
    build_hooks_with_tracker,
    check_memory_md_prerequisites,
)
from proposer.proposal import FileEdit, Proposal


@dataclass
class StubBeforeToolCall:
    tool_use: dict[str, Any]


@dataclass
class StubBeforeInvocation:
    pass


def fire_call(tracker: ProposerCallTracker, name: str) -> None:
    tracker.track(StubBeforeToolCall(tool_use={"name": name}))  # ty: ignore[invalid-argument-type]


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


def test_build_hooks_with_tracker_returns_a_pair() -> None:
    hooks, tracker = build_hooks_with_tracker()
    assert len(hooks) == 1
    assert hooks[0] is tracker


def test_tracker_records_tool_calls() -> None:
    _, tracker = build_hooks_with_tracker()
    fire_call(tracker, "read_memory_md")
    fire_call(tracker, "read_drift_report")
    assert tracker.called == {"read_memory_md", "read_drift_report"}


def test_tracker_resets_on_new_invocation() -> None:
    _, tracker = build_hooks_with_tracker()
    fire_call(tracker, "read_memory_md")
    tracker.reset(StubBeforeInvocation())  # ty: ignore[invalid-argument-type]
    assert tracker.called == set()


def test_memory_md_prerequisites_constant() -> None:
    assert MEMORY_MD_PREREQUISITES == ("read_memory_md", "read_drift_report")


def test_check_passes_when_no_memory_md_edits() -> None:
    _, tracker = build_hooks_with_tracker()
    proposal = Proposal(rationale="No edits this cycle.")
    assert check_memory_md_prerequisites(proposal, tracker) is None


def test_check_passes_when_memory_md_edit_and_both_reads_called() -> None:
    _, tracker = build_hooks_with_tracker()
    fire_call(tracker, "read_memory_md")
    fire_call(tracker, "read_drift_report")
    proposal = make_memory_md_proposal()
    assert check_memory_md_prerequisites(proposal, tracker) is None


def test_check_fails_when_memory_md_edit_and_no_reads() -> None:
    _, tracker = build_hooks_with_tracker()
    proposal = make_memory_md_proposal()
    reason = check_memory_md_prerequisites(proposal, tracker)
    assert reason is not None
    assert "read_memory_md" in reason
    assert "read_drift_report" in reason


def test_check_fails_when_only_memory_md_was_read() -> None:
    _, tracker = build_hooks_with_tracker()
    fire_call(tracker, "read_memory_md")
    # missing read_drift_report
    proposal = make_memory_md_proposal()
    reason = check_memory_md_prerequisites(proposal, tracker)
    assert reason is not None
    assert "read_drift_report" in reason


def test_proposal_pr_body_with_spec_dump_rejected() -> None:
    """The model_validator on Proposal must trip on `# Requirements`-style headers."""
    with pytest.raises(ValidationError):
        Proposal(
            rationale="Update conventions",
            pr_body=("Background\n\n# Requirements\n\nThe agent shall expose /healthz."),
        )


def test_proposal_pr_body_with_tasks_md_dump_rejected() -> None:
    with pytest.raises(ValidationError):
        Proposal(rationale="x", pr_body="text\n\n## tasks.md\n\nlist")


def test_proposal_pr_body_with_normal_design_text_passes() -> None:
    """Phrases like 'design considerations' must not trip the heuristic."""
    p = Proposal(
        rationale="Refactor proposal",
        pr_body="The design considerations for this change are minor.",
    )
    assert p.pr_body.startswith("The design")
