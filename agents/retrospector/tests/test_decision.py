"""Tests for LessonBullet / CaptureDecision / ConsolidationPlan invariants."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from retrospector.decision import (
    CaptureDecision,
    ConsolidationPlan,
    LessonBullet,
    MemoryAddition,
    SkillFile,
)


def memory_bullet_kwargs(**overrides: Any) -> dict[str, Any]:
    """A minimal valid ``memory_md`` LessonBullet — overridable per test."""
    base: dict[str, Any] = {
        "destination": "target_repo",
        "artifact_type": "memory_md",
        "scope": "MEMORY.md",
        "section": "conventions",
        "delta": "- **Frontend stack**: FastAPI + Jinja2 + Alpine.js.",
        "severity": 4,
        "generalizability": 4,
        "confidence": 0.85,
        "rationale": 'Reviewer @jplock said: "we use FastAPI + Jinja2, not React".',
        "evidence": ["https://github.com/owner/repo/pull/42#discussion_r1"],
    }
    return base | overrides


def skill_bullet_kwargs(**overrides: Any) -> dict[str, Any]:
    """A minimal valid ``skill_md`` LessonBullet — overridable per test."""
    base: dict[str, Any] = {
        "destination": "target_repo",
        "artifact_type": "skill_md",
        "scope": ".aidlc/skills/handle-pagination",
        "delta": "Use the existing pagination helper.",
        "severity": 3,
        "generalizability": 5,
        "confidence": 0.7,
        "rationale": "Implementer reinvented pagination twice; helper exists.",
        "evidence": ["s3://artifacts/runs/abc/validation/reviewer-r1.md"],
        "skill_name": "handle-pagination",
        "skill_description": "Use src/api/pagination.ts when adding new list endpoints.",
        "skill_body": "## Pagination\n\nUse the cursor-based helper at src/api/pagination.ts.\n",
    }
    return base | overrides


def test_memory_bullet_round_trips() -> None:
    bullet = LessonBullet(**memory_bullet_kwargs())
    assert bullet.artifact_type == "memory_md"
    assert bullet.section == "conventions"
    assert bullet.skill_name == ""


def test_skill_bullet_round_trips() -> None:
    bullet = LessonBullet(**skill_bullet_kwargs())
    assert bullet.artifact_type == "skill_md"
    assert bullet.section is None
    assert bullet.skill_name == "handle-pagination"


def test_memory_bullet_without_section_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_type=memory_md requires section"):
        LessonBullet(**memory_bullet_kwargs(section=None))


def test_memory_bullet_with_skill_fields_is_rejected() -> None:
    with pytest.raises(ValidationError, match="memory_md must not set skill_"):
        LessonBullet(**memory_bullet_kwargs(skill_name="oops"))


def test_skill_bullet_with_section_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_type=skill_md must not set section"):
        LessonBullet(**skill_bullet_kwargs(section="conventions"))


def test_skill_bullet_without_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_type=skill_md requires skill_name"):
        LessonBullet(**skill_bullet_kwargs(skill_name=""))


def test_skill_bullet_without_description_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_type=skill_md requires skill_description"):
        LessonBullet(**skill_bullet_kwargs(skill_description=""))


def test_skill_bullet_without_body_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_type=skill_md requires skill_body"):
        LessonBullet(**skill_bullet_kwargs(skill_body=""))


def test_score_fields_enforce_bounds() -> None:
    with pytest.raises(ValidationError):
        LessonBullet(**memory_bullet_kwargs(severity=6))
    with pytest.raises(ValidationError):
        LessonBullet(**memory_bullet_kwargs(generalizability=0))
    with pytest.raises(ValidationError):
        LessonBullet(**memory_bullet_kwargs(confidence=1.5))


def test_capture_decision_empty_bullets_is_valid() -> None:
    decision = CaptureDecision(rationale="Clean merge, nothing to record.")
    assert decision.bullets == []


def test_capture_decision_carries_bullets() -> None:
    decision = CaptureDecision(
        bullets=[
            LessonBullet(**memory_bullet_kwargs()),
            LessonBullet(**skill_bullet_kwargs()),
        ],
        rationale="Two bullets from a request_changes verdict.",
    )
    assert len(decision.bullets) == 2
    assert {b.artifact_type for b in decision.bullets} == {"memory_md", "skill_md"}


def test_consolidation_plan_round_trips() -> None:
    plan = ConsolidationPlan(
        memory_additions=[
            MemoryAddition(
                scope="MEMORY.md",
                section="conventions",
                addition="- **Frontend stack**: FastAPI + Jinja2 + Alpine.js.",
            ),
        ],
        skill_files=[
            SkillFile(
                scope=".aidlc/skills/handle-pagination",
                name="handle-pagination",
                description="Use src/api/pagination.ts when adding new list endpoints.",
                body="## Pagination\n\nUse the cursor-based helper.\n",
            ),
        ],
        shipped_event_ids=["evt-1", "evt-2"],
        discarded_event_ids=["evt-3"],
        rationale="Two bullets converged; one was noise.",
    )
    assert plan.shipped_event_ids == ["evt-1", "evt-2"]
    assert plan.discarded_event_ids == ["evt-3"]


def test_consolidation_plan_allows_empty_lists() -> None:
    """An empty plan is valid — the consolidate run might defer everything."""
    plan = ConsolidationPlan(rationale="All bullets deferred for next week.")
    assert plan.memory_additions == []
    assert plan.skill_files == []
    assert plan.shipped_event_ids == []
    assert plan.discarded_event_ids == []
