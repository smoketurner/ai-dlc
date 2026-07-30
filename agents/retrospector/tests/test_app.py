"""Tests for retrospector.app — capture write, consolidate read+PR+delete."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from common.agentcore_memory import MemoryEvent, StoredEvent
from common.runtime import RetrospectorInput
from retrospector import app as retrospector_app
from retrospector.app import (
    branch_name,
    delete_consumed_events,
    fetch_file,
    memory_files_for,
    open_consolidation_prs,
    render_events_as_buffer,
    render_pr_body,
    render_skill_file,
    run_capture,
    run_consolidate,
    session_id_for,
    write_bullets,
)
from retrospector.decision import (
    CaptureDecision,
    ConsolidationPlan,
    LessonBullet,
    MemoryAddition,
    SkillFile,
)

EXISTING_MEMORY_MD = """\
# Project Memory

Intro paragraph.

## Overview

Project summary.

## Conventions

- Use Python 3.14.

## Decisions

## Constraints

- Run on arm64 only.

## Glossary

## Notes
"""


# --- helpers ---------------------------------------------------------------


def make_payload(**overrides: Any) -> RetrospectorInput:
    base: dict[str, Any] = {
        "mode": "capture",
        "event_type": "REVIEW.READY",
        "project_slug": "ai-dlc",
        "target_repo": "smoketurner/ai-dlc",
        "pr_url": "https://github.com/smoketurner/ai-dlc/pull/42",
        "verdict": "request_changes",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
        "correlation_id": "019e0e69-198d-7263-8bfc-7eb9e8ae05df",
    }
    base.update(overrides)
    return RetrospectorInput(**base)


def make_memory_bullet(**overrides: Any) -> LessonBullet:
    base: dict[str, Any] = {
        "destination": "target_repo",
        "artifact_type": "memory_md",
        "scope": "MEMORY.md",
        "section": "conventions",
        "delta": "- Use FastAPI + Jinja2.",
        "severity": 4,
        "generalizability": 4,
        "confidence": 0.85,
        "rationale": "Reviewer rejected SPA in PR #42.",
        "evidence": ["https://github.com/smoketurner/ai-dlc/pull/42#discussion_r1"],
    }
    base.update(overrides)
    return LessonBullet(**base)


def make_skill_bullet(**overrides: Any) -> LessonBullet:
    base: dict[str, Any] = {
        "destination": "target_repo",
        "artifact_type": "skill_md",
        "scope": ".aidlc/skills/handle-pagination",
        "delta": "Use src/api/pagination.ts helper.",
        "severity": 3,
        "generalizability": 5,
        "confidence": 0.7,
        "rationale": "Implementer reinvented pagination twice.",
        "evidence": ["s3://artifacts/runs/abc/validation/reviewer-r1.md"],
        "skill_name": "handle-pagination",
        "skill_description": "Use src/api/pagination.ts when adding new list endpoints.",
        "skill_body": "## Pagination\n\nUse the cursor-based helper.\n",
    }
    base.update(overrides)
    return LessonBullet(**base)


def make_stored_event(event_id: str, *, bullet: LessonBullet, when: dt.datetime) -> StoredEvent:
    text = json.dumps(
        {
            "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
            "event_type": "REVIEW.READY",
            "verdict": "request_changes",
            "bullet": bullet.model_dump(),
        },
        sort_keys=True,
    )
    return StoredEvent(
        event_id=event_id,
        actor_id="retrospector",
        session_id="pending_lessons:target:ai-dlc",
        timestamp=when,
        text=text,
    )


# --- session keying --------------------------------------------------------


def test_session_id_for_target_uses_sanitised_slug() -> None:
    assert session_id_for(destination="target_repo", project_slug="ai-dlc") == (
        "pending_lessons:target:ai-dlc"
    )


def test_session_id_for_platform_is_fixed() -> None:
    assert session_id_for(destination="platform", project_slug="ignored") == (
        "pending_lessons:platform"
    )


def test_session_id_for_sanitises_unsafe_chars() -> None:
    """Multiple unsafe chars collapse to a single hyphen — the regex is greedy."""
    assert session_id_for(destination="target_repo", project_slug="Acme/Project_#1") == (
        "pending_lessons:target:acme-project-1"
    )


# --- capture: write each bullet as one memory event ------------------------


def test_write_bullets_calls_create_event_per_bullet(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = make_payload()
    create_mock = MagicMock()
    monkeypatch.setattr(retrospector_app, "create_event", create_mock)
    monkeypatch.setattr(retrospector_app, "agentcore_client", MagicMock())
    monkeypatch.setattr(retrospector_app, "memory_id", lambda: "mem-1")

    counts = write_bullets(
        bullets=[make_memory_bullet(), make_skill_bullet()],
        payload=payload,
    )

    assert counts == {"target_repo": 2}
    assert create_mock.call_count == 2
    first_call = create_mock.call_args_list[0]
    assert first_call.kwargs["memory_id"] == "mem-1"
    assert first_call.kwargs["actor_id"] == "retrospector"
    assert first_call.kwargs["session_id"] == "pending_lessons:target:ai-dlc"
    events: list[MemoryEvent] = first_call.kwargs["events"]
    assert events[0].role == "TOOL"
    assert "FastAPI + Jinja2" in events[0].text


def test_write_bullets_routes_each_destination_to_its_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bullet for ``platform`` ends up in a different session than ``target_repo``."""
    payload = make_payload()
    create_mock = MagicMock()
    monkeypatch.setattr(retrospector_app, "create_event", create_mock)
    monkeypatch.setattr(retrospector_app, "agentcore_client", MagicMock())
    monkeypatch.setattr(retrospector_app, "memory_id", lambda: "mem-1")

    counts = write_bullets(
        bullets=[make_memory_bullet(), make_memory_bullet(destination="platform")],
        payload=payload,
    )

    assert counts == {"target_repo": 1, "platform": 1}
    sessions = {call.kwargs["session_id"] for call in create_mock.call_args_list}
    assert sessions == {"pending_lessons:target:ai-dlc", "pending_lessons:platform"}


def test_run_capture_skips_when_no_bullets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        retrospector_app,
        "capture",
        lambda *args, **kwargs: CaptureDecision(rationale="Clean event."),
    )
    create_mock = MagicMock()
    monkeypatch.setattr(retrospector_app, "create_event", create_mock)
    monkeypatch.setattr(retrospector_app, "agentcore_client", MagicMock())
    monkeypatch.setattr(retrospector_app, "memory_id", lambda: "mem-1")

    run_capture(MagicMock(), payload=make_payload())

    create_mock.assert_not_called()


# --- consolidate: list → run → open PRs → delete shipped+discarded --------


def test_render_events_as_buffer_sorts_by_timestamp() -> None:
    later = dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC)
    earlier = dt.datetime(2026, 5, 15, 10, 0, tzinfo=dt.UTC)
    events = [
        make_stored_event("evt-later", bullet=make_memory_bullet(), when=later),
        make_stored_event("evt-earlier", bullet=make_skill_bullet(), when=earlier),
    ]
    buffer = render_events_as_buffer(events)
    assert buffer.index("evt-earlier") < buffer.index("evt-later")
    assert "FastAPI + Jinja2" in buffer
    assert "handle-pagination" in buffer


def test_delete_consumed_events_swallows_per_event_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failed delete shouldn't stop the others — best-effort cleanup."""
    delete_mock = MagicMock(side_effect=[None, RuntimeError("boom"), None])
    monkeypatch.setattr(retrospector_app, "delete_event", delete_mock)
    monkeypatch.setattr(retrospector_app, "agentcore_client", MagicMock())
    monkeypatch.setattr(retrospector_app, "memory_id", lambda: "mem-1")

    removed = delete_consumed_events(
        session="pending_lessons:platform",
        event_ids=["evt-1", "evt-2", "evt-3"],
    )

    assert removed == 2
    assert delete_mock.call_count == 3


def test_run_consolidate_requires_destination() -> None:
    payload = make_payload(
        mode="consolidate",
        event_type="SCHEDULED.LESSONS_CONSOLIDATE",
        destination=None,
    )
    with pytest.raises(ValueError, match="destination"):
        run_consolidate(MagicMock(), payload=payload, mcp_client=MagicMock())


def test_run_consolidate_no_events_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrospector_app, "list_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(retrospector_app, "agentcore_client", MagicMock())
    monkeypatch.setattr(retrospector_app, "memory_id", lambda: "mem-1")
    consolidate_mock = MagicMock()
    monkeypatch.setattr(retrospector_app, "consolidate", consolidate_mock)

    payload = make_payload(
        mode="consolidate",
        event_type="SCHEDULED.LESSONS_CONSOLIDATE",
        destination="target_repo",
    )
    run_consolidate(MagicMock(), payload=payload, mcp_client=MagicMock())

    consolidate_mock.assert_not_called()


def test_memory_files_for_appends_to_existing_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        return_value={
            "ok": True,
            "result": {"exists": True, "content": EXISTING_MEMORY_MD, "sha": "abc", "ref": "main"},
        },
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    payload = make_payload()
    additions = [
        MemoryAddition(
            scope="MEMORY.md",
            section="conventions",
            addition="- **Frontend stack**: FastAPI + Jinja2.",
        ),
    ]
    files, scopes_written = memory_files_for(
        MagicMock(),
        payload=payload,
        additions=additions,
    )
    assert len(files) == 1
    assert files[0]["path"] == "MEMORY.md"
    assert "Use Python 3.14" in files[0]["content"]  # existing bullet survives
    assert "Frontend stack" in files[0]["content"]
    assert scopes_written == {"MEMORY.md"}


def test_memory_files_for_groups_by_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock(
        return_value={
            "ok": True,
            "result": {"exists": False, "content": "", "sha": "", "ref": "main"},
        },
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    payload = make_payload()
    additions = [
        MemoryAddition(scope="MEMORY.md", section="conventions", addition="- Root rule."),
        MemoryAddition(
            scope="src/api/MEMORY.md",
            section="conventions",
            addition="- API rule.",
        ),
    ]
    files, scopes_written = memory_files_for(
        MagicMock(),
        payload=payload,
        additions=additions,
    )
    paths = sorted(f["path"] for f in files)
    assert paths == ["MEMORY.md", "src/api/MEMORY.md"]
    assert scopes_written == {"MEMORY.md", "src/api/MEMORY.md"}


MALFORMED_MEMORY_MD = """\
# Project Memory

## Conventions

- Sections are out of canonical order.

## Overview

Project summary.
"""


def test_memory_files_for_skips_malformed_scope_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unparseable MEMORY.md must not wedge the whole consolidation.

    The malformed scope is dropped from ``files`` AND from
    ``scopes_written`` so the caller can detect the partial failure and
    avoid deleting events whose lessons were never written to a PR.
    """
    contents = {
        "MEMORY.md": MALFORMED_MEMORY_MD,
        "src/api/MEMORY.md": EXISTING_MEMORY_MD,
    }

    def fake(_mcp_client: Any, **kw: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "exists": True,
                "content": contents[kw["path"]],
                "sha": "abc",
                "ref": "main",
            },
        }

    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    additions = [
        MemoryAddition(scope="MEMORY.md", section="conventions", addition="- Root rule."),
        MemoryAddition(scope="src/api/MEMORY.md", section="conventions", addition="- API rule."),
    ]

    files, scopes_written = memory_files_for(
        MagicMock(),
        payload=make_payload(),
        additions=additions,
    )

    assert [f["path"] for f in files] == ["src/api/MEMORY.md"]
    assert "API rule" in files[0]["content"]
    assert scopes_written == {"src/api/MEMORY.md"}
    assert "MEMORY.md" not in scopes_written


def test_memory_files_for_returns_empty_when_every_scope_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        return_value={
            "ok": True,
            "result": {
                "exists": True,
                "content": MALFORMED_MEMORY_MD,
                "sha": "abc",
                "ref": "main",
            },
        },
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    additions = [MemoryAddition(scope="MEMORY.md", section="conventions", addition="- Rule.")]

    files, scopes_written = memory_files_for(
        MagicMock(),
        payload=make_payload(),
        additions=additions,
    )

    assert files == []
    assert scopes_written == set()


def test_render_skill_file_emits_frontmatter() -> None:
    skill = SkillFile(
        scope=".aidlc/skills/handle-pagination",
        name="handle-pagination",
        description="Use src/api/pagination.ts when adding new list endpoints.",
        body="## Pagination\n\nUse the cursor-based helper.",
    )
    out = render_skill_file(skill)
    assert out.startswith("---\n")
    assert "name: handle-pagination" in out
    assert "description: Use src/api/pagination.ts" in out
    assert "## Pagination" in out
    assert out.endswith("\n")


def test_skill_file_scope_rejects_md_suffix() -> None:
    """Stray `.md` on scope is a contract violation — the platform appends SKILL.md."""
    with pytest.raises(ValidationError, match="slug folder path"):
        SkillFile(
            scope=".aidlc/skills/handle-pagination.md",
            name="handle-pagination",
            description="x",
            body="y",
        )


def test_open_consolidation_prs_opens_zero_when_plan_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        retrospector_app,
        "invoke_repo_helper",
        MagicMock(side_effect=AssertionError("should not be called")),
    )
    payload = make_payload(mode="consolidate", destination="target_repo")
    plan = ConsolidationPlan(rationale="Everything deferred.")
    pr_urls, scopes_written = open_consolidation_prs(
        MagicMock(),
        payload=payload,
        plan=plan,
    )
    assert pr_urls == []
    assert scopes_written == set()


def test_open_consolidation_prs_opens_two_when_both_artifact_types_ship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        side_effect=[
            # memory PR: get_file → create_branch → commit_files → open_pr
            {"ok": True, "result": {"exists": True, "content": EXISTING_MEMORY_MD, "ref": "main"}},
            {"ok": True, "result": {"branch": "x"}},
            {"ok": True, "result": {"commit_sha": "newsha"}},
            {"ok": True, "result": {"pr_url": "https://x/pr/1"}},
            # skill PR: create_branch → commit_files → open_pr (no get_file)
            {"ok": True, "result": {"branch": "y"}},
            {"ok": True, "result": {"commit_sha": "newsha2"}},
            {"ok": True, "result": {"pr_url": "https://x/pr/2"}},
        ],
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    payload = make_payload(mode="consolidate", destination="target_repo")
    plan = ConsolidationPlan(
        memory_additions=[
            MemoryAddition(
                scope="MEMORY.md",
                section="conventions",
                addition="- New rule.",
            ),
        ],
        skill_files=[
            SkillFile(
                scope=".aidlc/skills/foo",
                name="foo",
                description="Use foo when bar.",
                body="Body.",
            ),
        ],
        rationale="One of each.",
    )
    pr_urls, scopes_written = open_consolidation_prs(
        MagicMock(),
        payload=payload,
        plan=plan,
    )
    assert pr_urls == ["https://x/pr/1", "https://x/pr/2"]
    assert scopes_written == {"MEMORY.md"}


def test_open_consolidation_prs_returns_scopes_written_empty_when_all_scopes_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every scope skipped → no memory PR opened, scopes_written empty.

    The caller uses an empty ``scopes_written`` (against non-empty
    ``memory_additions``) to detect total failure and avoid deleting
    shipped events.
    """
    fake = MagicMock(
        side_effect=[
            # get_file for the one malformed scope; no further repo_helper
            # calls expected (no create_branch / commit_files / open_pr).
            {"ok": True, "result": {"exists": True, "content": MALFORMED_MEMORY_MD, "ref": "main"}},
        ],
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    payload = make_payload(mode="consolidate", destination="target_repo")
    plan = ConsolidationPlan(
        memory_additions=[
            MemoryAddition(scope="MEMORY.md", section="conventions", addition="- Rule."),
        ],
        rationale="Ship it.",
    )
    pr_urls, scopes_written = open_consolidation_prs(
        MagicMock(),
        payload=payload,
        plan=plan,
    )
    assert pr_urls == []
    assert scopes_written == set()


def _stub_consolidate_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: ConsolidationPlan,
    stored_events: list[StoredEvent],
) -> MagicMock:
    """Wire up the run_consolidate dependencies to use ``plan`` and ``events``.

    Returns the ``delete_event`` mock so the caller can assert which event
    IDs were deleted.
    """
    monkeypatch.setattr(
        retrospector_app,
        "list_events",
        lambda *args, **kwargs: stored_events,
    )
    monkeypatch.setattr(retrospector_app, "agentcore_client", MagicMock())
    monkeypatch.setattr(retrospector_app, "memory_id", lambda: "mem-1")
    monkeypatch.setattr(
        retrospector_app,
        "consolidate",
        lambda *args, **kwargs: plan,
    )
    delete_mock = MagicMock()
    monkeypatch.setattr(retrospector_app, "delete_event", delete_mock)
    return delete_mock


def test_run_consolidate_deletes_shipped_and_discarded_when_all_scopes_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: all scopes written → both shipped and discarded events deleted."""
    plan = ConsolidationPlan(
        memory_additions=[
            MemoryAddition(scope="MEMORY.md", section="conventions", addition="- Rule."),
        ],
        shipped_event_ids=["evt-ship-1", "evt-ship-2"],
        discarded_event_ids=["evt-disc-1"],
        rationale="All good.",
    )
    events = [
        make_stored_event(
            "evt-ship-1",
            bullet=make_memory_bullet(),
            when=dt.datetime(2026, 5, 15, 10, 0, tzinfo=dt.UTC),
        ),
        make_stored_event(
            "evt-ship-2",
            bullet=make_memory_bullet(delta="- Another."),
            when=dt.datetime(2026, 5, 15, 11, 0, tzinfo=dt.UTC),
        ),
        make_stored_event(
            "evt-disc-1",
            bullet=make_memory_bullet(delta="- Noisy."),
            when=dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC),
        ),
    ]
    delete_mock = _stub_consolidate_flow(monkeypatch, plan=plan, stored_events=events)
    monkeypatch.setattr(
        retrospector_app,
        "invoke_repo_helper",
        MagicMock(
            side_effect=[
                {
                    "ok": True,
                    "result": {"exists": True, "content": EXISTING_MEMORY_MD, "ref": "main"},
                },
                {"ok": True, "result": {"branch": "x"}},
                {"ok": True, "result": {"commit_sha": "newsha"}},
                {"ok": True, "result": {"pr_url": "https://x/pr/1"}},
            ],
        ),
    )

    payload = make_payload(
        mode="consolidate",
        event_type="SCHEDULED.LESSONS_CONSOLIDATE",
        destination="target_repo",
    )
    run_consolidate(MagicMock(), payload=payload, mcp_client=MagicMock())

    deleted_ids = {call.kwargs["event_id"] for call in delete_mock.call_args_list}
    assert deleted_ids == {"evt-ship-1", "evt-ship-2", "evt-disc-1"}


def test_run_consolidate_skips_shipped_event_deletion_on_partial_scope_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial scope failure → shipped events NOT deleted (data-loss fix).

    One scope's MEMORY.md is malformed, the other is valid. The PR opens
    for the valid scope, but the shipped events (which include events for
    the malformed scope) must NOT be deleted — they resurface next week.
    Discarded events are still deleted (the agent explicitly dropped them).
    """
    plan = ConsolidationPlan(
        memory_additions=[
            MemoryAddition(scope="MEMORY.md", section="conventions", addition="- Root rule."),
            MemoryAddition(
                scope="src/api/MEMORY.md",
                section="conventions",
                addition="- API rule.",
            ),
        ],
        shipped_event_ids=["evt-root", "evt-api"],
        discarded_event_ids=["evt-noise"],
        rationale="Two scopes, one malformed.",
    )
    events = [
        make_stored_event(
            "evt-root",
            bullet=make_memory_bullet(),
            when=dt.datetime(2026, 5, 15, 10, 0, tzinfo=dt.UTC),
        ),
        make_stored_event(
            "evt-api",
            bullet=make_memory_bullet(delta="- API lesson."),
            when=dt.datetime(2026, 5, 15, 11, 0, tzinfo=dt.UTC),
        ),
        make_stored_event(
            "evt-noise",
            bullet=make_memory_bullet(delta="- Noise."),
            when=dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.UTC),
        ),
    ]
    delete_mock = _stub_consolidate_flow(monkeypatch, plan=plan, stored_events=events)
    # get_file(MEMORY.md=malformed) → get_file(src/api=valid) →
    # create_branch → commit_files → open_pr
    monkeypatch.setattr(
        retrospector_app,
        "invoke_repo_helper",
        MagicMock(
            side_effect=[
                {
                    "ok": True,
                    "result": {"exists": True, "content": MALFORMED_MEMORY_MD, "ref": "main"},
                },
                {
                    "ok": True,
                    "result": {"exists": True, "content": EXISTING_MEMORY_MD, "ref": "main"},
                },
                {"ok": True, "result": {"branch": "x"}},
                {"ok": True, "result": {"commit_sha": "newsha"}},
                {"ok": True, "result": {"pr_url": "https://x/pr/1"}},
            ],
        ),
    )

    payload = make_payload(
        mode="consolidate",
        event_type="SCHEDULED.LESSONS_CONSOLIDATE",
        destination="target_repo",
    )
    run_consolidate(MagicMock(), payload=payload, mcp_client=MagicMock())

    deleted_ids = {call.kwargs["event_id"] for call in delete_mock.call_args_list}
    assert deleted_ids == {"evt-noise"}
    assert "evt-root" not in deleted_ids
    assert "evt-api" not in deleted_ids


def test_run_consolidate_deletes_all_events_when_no_memory_additions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skills-only plan → no memory scopes → all shipped events deleted.

    With no ``memory_additions``, ``intended_scopes`` is empty and equals
    ``scopes_written`` (also empty), so the gating check passes and
    shipped events are deleted normally.
    """
    plan = ConsolidationPlan(
        skill_files=[
            SkillFile(
                scope=".aidlc/skills/foo",
                name="foo",
                description="Use foo when bar.",
                body="Body.",
            ),
        ],
        shipped_event_ids=["evt-skill"],
        discarded_event_ids=["evt-disc"],
        rationale="Skills only.",
    )
    events = [
        make_stored_event(
            "evt-skill",
            bullet=make_skill_bullet(),
            when=dt.datetime(2026, 5, 15, 10, 0, tzinfo=dt.UTC),
        ),
        make_stored_event(
            "evt-disc",
            bullet=make_memory_bullet(delta="- Noise."),
            when=dt.datetime(2026, 5, 15, 11, 0, tzinfo=dt.UTC),
        ),
    ]
    delete_mock = _stub_consolidate_flow(monkeypatch, plan=plan, stored_events=events)
    monkeypatch.setattr(
        retrospector_app,
        "invoke_repo_helper",
        MagicMock(
            side_effect=[
                {"ok": True, "result": {"branch": "x"}},
                {"ok": True, "result": {"commit_sha": "newsha"}},
                {"ok": True, "result": {"pr_url": "https://x/pr/1"}},
            ],
        ),
    )

    payload = make_payload(
        mode="consolidate",
        event_type="SCHEDULED.LESSONS_CONSOLIDATE",
        destination="target_repo",
    )
    run_consolidate(MagicMock(), payload=payload, mcp_client=MagicMock())

    deleted_ids = {call.kwargs["event_id"] for call in delete_mock.call_args_list}
    assert deleted_ids == {"evt-skill", "evt-disc"}


# --- shared helpers --------------------------------------------------------


def test_branch_name_is_deterministic_and_scoped_by_destination() -> None:
    payload = make_payload(mode="consolidate", destination="target_repo")
    branch = branch_name(payload=payload, kind="memory", timestamp="20260515-090000")
    assert branch == "retrospective/target_repo/20260515-090000-memory"


def test_fetch_file_returns_empty_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock(
        return_value={
            "ok": True,
            "result": {"exists": False, "content": "", "sha": "", "ref": "main"},
        },
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    assert fetch_file(MagicMock(), repo="o/r", path="MISSING.md") == ""


def test_render_pr_body_quotes_rationale_and_lists_changes() -> None:
    payload = make_payload(mode="consolidate", destination="platform")
    plan = ConsolidationPlan(
        memory_additions=[
            MemoryAddition(
                scope="MEMORY.md",
                section="conventions",
                addition="- New.",
            ),
        ],
        rationale="Two bullets converged.",
    )
    body = render_pr_body(payload=payload, plan=plan, title_kind="memory")
    assert "**platform**" in body
    assert "Two bullets converged." in body
    assert "`MEMORY.md`" in body
    assert "`conventions`" in body
