"""Tests for proposer.proposal — Pydantic validation + target-file allowlist."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proposer.proposal import FileEdit, Proposal


def test_proposal_with_no_edits_validates() -> None:
    p = Proposal(
        rationale="Pass rate steady at 92%; rejection categories unchanged. Holding off.",
        supporting_evidence=["evals/results lookback=30d total=24"],
    )
    assert p.edits == []
    assert p.pr_title == "ai-dlc proposer: no-op"


def test_proposal_with_memory_md_edit() -> None:
    edit = FileEdit(
        target_file="docs/MEMORY.md",
        proposed_content="# Conventions\n- Use structlog.",
    )
    p = Proposal(
        rationale="Few-shot bank shows structlog use across 8 successful runs.",
        supporting_evidence=["few-shots/intent_to_spec total=8"],
        edits=[edit],
        pr_title="proposer: document structlog convention",
        pr_body="The few-shot bank shows...",
    )
    assert len(p.edits) == 1


def test_proposal_with_prompts_b_edit() -> None:
    edit = FileEdit(
        target_file="agents/architect/src/architect/prompts_b.py",
        proposed_content='SYSTEM_PROMPT = """new variant"""',
    )
    p = Proposal(
        rationale="A/B-testing a more concise architect prompt.",
        edits=[edit],
        pr_title="proposer: A/B test architect prompts_b",
        pr_body="...",
    )
    assert p.edits[0].target_file.endswith("prompts_b.py")


def test_disallowed_target_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit(
            target_file="terraform/modules/agents/variables.tf",
            proposed_content="x",
        )


def test_disallowed_path_outside_agents_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit(
            target_file="services/dashboard/src/dashboard/app.py",
            proposed_content="x",
        )


def test_disallowed_random_md_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit(
            target_file="docs/ROADMAP.md",
            proposed_content="x",
        )


def test_max_edits_enforced() -> None:
    edits = [
        FileEdit(
            target_file="agents/architect/src/architect/prompts.py",
            proposed_content=f"# v{i}",
        )
        for i in range(9)  # one over max
    ]
    with pytest.raises(ValidationError):
        Proposal(rationale="too many edits", edits=edits, pr_title="x", pr_body="x")


def test_proposal_is_frozen() -> None:
    p = Proposal(rationale="x")
    with pytest.raises(ValidationError):
        p.rationale = "y"  # type: ignore[misc]  # frozen=True forbids assignment
