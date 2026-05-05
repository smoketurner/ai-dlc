"""Tests for ``common.personas`` — shared persona snippets."""

from __future__ import annotations

from common.personas import (
    DOOR_TAXONOMY,
    MEMORY_MD_DISCIPLINE,
    PR_PROSE_VOCABULARY_BAN,
    coordination_footer,
)


def test_door_taxonomy_lists_all_ten_categories() -> None:
    expected = [
        "schema_migration",
        "public_api_break",
        "production_terraform",
        "iam_authorization",
        "auth_flow",
        "cryptography_or_secrets",
        "major_dependency_bump",
        "scheduled_job",
        "event_schema_breaking",
        "public_deletion",
    ]
    for category in expected:
        assert f"``{category}``" in DOOR_TAXONOMY


def test_pr_prose_vocabulary_ban_lists_banned_words() -> None:
    for word in ("critical", "crucial", "essential", "significant", "comprehensive"):
        assert f"``{word}``" in PR_PROSE_VOCABULARY_BAN


def test_memory_md_discipline_mentions_conventions_section() -> None:
    assert "MEMORY.md" in MEMORY_MD_DISCIPLINE
    assert "Conventions" in MEMORY_MD_DISCIPLINE


def test_coordination_footer_includes_all_fields() -> None:
    out = coordination_footer(
        role="Reviewer",
        predecessor="Implementer (per-task PR opened)",
        expected_context="A PR with diff_summary, the spec_slug, and the task_id.",
        focus="Anchored review comments and a verdict against the spec's acceptance criteria.",
    )
    assert "Coordination (Reviewer):" in out
    assert "Predecessor: Implementer" in out
    assert "Expected context: A PR" in out
    assert "Focus: Anchored review" in out


def test_coordination_footer_has_consistent_indent() -> None:
    out = coordination_footer(
        role="X",
        predecessor="Y",
        expected_context="Z",
        focus="W",
    )
    assert out.endswith("\n")
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[1].startswith("  - Predecessor:")
    assert lines[2].startswith("  - Expected context:")
    assert lines[3].startswith("  - Focus:")
