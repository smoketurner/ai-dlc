"""Tests for proposer.proposal — Pydantic validation + target-file allowlist."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proposer.proposal import FileEdit, Proposal, ProposedIssue


def test_proposal_with_no_edits_validates() -> None:
    p = Proposal(
        rationale="No actionable signal in the issue's references; holding off.",
        supporting_evidence=["https://example.com/post"],
    )
    assert p.edits == []
    assert p.pr_title == "proposer: no-op"


def test_proposal_with_memory_md_edit() -> None:
    edit = FileEdit(
        target_file="docs/MEMORY.md",
        proposed_content="# Conventions\n- Use structlog.",
    )
    p = Proposal(
        rationale="Stripe Minions blog recommends structured logs at agent boundaries.",
        supporting_evidence=[
            "https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents"
        ],
        edits=[edit],
        pr_title="proposer: document structlog convention",
        pr_body="Source post recommends structlog at every agent boundary.",
    )
    assert len(p.edits) == 1


def test_proposal_with_agents_md_edit() -> None:
    edit = FileEdit(
        target_file="AGENTS.md",
        proposed_content="# Project conventions\n- Use structlog.",
    )
    p = Proposal(
        rationale="Reviewer expressed a structured-logging preference worth recording.",
        edits=[edit],
        pr_title="proposer: record structlog convention",
        pr_body="Adds the convention to AGENTS.md after PR feedback.",
    )
    assert p.edits[0].target_file == "AGENTS.md"


def test_disallowed_prompts_file_rejected() -> None:
    """Agent prompts files are no longer in the proposer's allowed set."""
    with pytest.raises(ValidationError):
        FileEdit(
            target_file="agents/architect/src/architect/prompts.py",
            proposed_content="x",
        )


def test_disallowed_terraform_target_rejected() -> None:
    with pytest.raises(ValidationError):
        FileEdit(
            target_file="terraform/modules/agents/variables.tf",
            proposed_content="x",
        )


def test_disallowed_source_file_rejected() -> None:
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
            target_file="docs/MEMORY.md",
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
