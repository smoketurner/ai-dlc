"""Tests for proposer.proposal — Pydantic validation + target-file allowlist."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proposer.proposal import FileEdit, Proposal, ProposedIssue


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


def test_proposal_with_proposed_issues() -> None:
    p = Proposal(
        rationale="user asked us to spawn issues for top adopt items",
        summary_comment="Created 2 issues for the highest-impact recommendations.",
        proposed_issues=[
            ProposedIssue(
                title="Adopt scoped rule files split by directory",
                body="## Scope\nSplit MEMORY.md by subdirectory.\n\n## Acceptance\n- ...",
                labels=["aidlc-spawned", "adopt"],
            ),
            ProposedIssue(
                title="Pre-warm sandbox snapshots",
                body="## Scope\nUse Modal-style snapshots.\n\n## Acceptance\n- ...",
                labels=["aidlc-spawned", "adopt"],
            ),
        ],
    )
    assert len(p.proposed_issues) == 2
    assert p.proposed_issues[0].title.startswith("Adopt")


def test_proposed_issue_requires_title_and_body() -> None:
    with pytest.raises(ValidationError):
        ProposedIssue(title="", body="x")
    with pytest.raises(ValidationError):
        ProposedIssue(title="x", body="")


def test_proposed_issue_max_labels_enforced() -> None:
    with pytest.raises(ValidationError):
        ProposedIssue(title="x", body="y", labels=[f"l{i}" for i in range(9)])


def test_proposal_max_proposed_issues_enforced() -> None:
    issues = [
        ProposedIssue(title=f"Issue {i}", body="body")
        for i in range(17)  # one over max
    ]
    with pytest.raises(ValidationError):
        Proposal(rationale="too many issues", proposed_issues=issues)
