"""Tests for retrospector.app — patch logic + PR-opening flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import RetrospectorInput
from retrospector import app as retrospector_app
from retrospector.app import (
    branch_name,
    open_memory_md_pr,
    render_memory_md_patch,
    render_pr_body,
    render_pr_title,
)
from retrospector.decision import RetrospectiveDecision

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


def make_payload(**overrides: Any) -> RetrospectorInput:
    base: dict[str, Any] = {
        "event_type": "TASK.APPROVED",
        "project_slug": "ai-dlc",
        "target_repo": "smoketurner/ai-dlc",
        "pr_url": "https://github.com/smoketurner/ai-dlc/pull/42",
        "spec_slug": "lint-gate",
        "task_id": "T-001",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
        "correlation_id": "019e0e69-198d-7263-8bfc-7eb9e8ae05df",
    }
    base.update(overrides)
    return RetrospectorInput(**base)


def make_decision(**overrides: Any) -> RetrospectiveDecision:
    base: dict[str, Any] = {
        "has_lesson": True,
        "section": "conventions",
        "lesson_summary": "Use FastAPI + Jinja2, not React",
        "memory_md_addition": "- **Frontend stack**: FastAPI + Jinja2 + Alpine.js (CDN).",
        "rationale": "Reviewer @jplock rejected SPA in PR #42.",
        "confidence": 0.85,
    }
    base.update(overrides)
    return RetrospectiveDecision(**base)


def test_render_memory_md_patch_appends_under_named_section() -> None:
    out = render_memory_md_patch(
        existing=EXISTING_MEMORY_MD,
        section="conventions",
        addition="- Use FastAPI + Jinja2.",
    )
    # The new bullet lands inside the Conventions section, before Decisions.
    conventions_idx = out.index("## Conventions")
    decisions_idx = out.index("## Decisions")
    new_bullet_idx = out.index("Use FastAPI + Jinja2")
    assert conventions_idx < new_bullet_idx < decisions_idx
    # Existing Conventions bullet survives.
    assert "Use Python 3.14" in out


def test_render_memory_md_patch_seeds_canonical_doc_when_existing_empty() -> None:
    """No mirror yet — start from the default MemoryDoc and add the bullet."""
    out = render_memory_md_patch(
        existing="",
        section="constraints",
        addition="- Run on arm64 only.",
    )
    assert out.startswith("# Project Memory")
    # Default doc has all six sections; the addition should land under
    # Constraints, between Decisions and Glossary.
    decisions_idx = out.index("## Decisions")
    constraints_idx = out.index("## Constraints")
    glossary_idx = out.index("## Glossary")
    bullet_idx = out.index("Run on arm64 only")
    assert decisions_idx < constraints_idx < bullet_idx < glossary_idx


def test_render_pr_title_caps_at_72_chars() -> None:
    decision = make_decision(
        lesson_summary="A" * 200,  # well above the truncation limit
    )
    title = render_pr_title(decision)
    assert len(title) <= 72
    assert title.startswith("retrospective: ")


def test_render_pr_body_quotes_rationale_and_links_pr() -> None:
    payload = make_payload(pr_url="https://github.com/o/r/pull/9")
    decision = make_decision()
    body = render_pr_body(payload=payload, decision=decision)
    assert "**Lesson:**" in body
    assert decision.lesson_summary in body
    assert "**Rationale:**" in body
    assert decision.rationale in body
    assert "Source PR: https://github.com/o/r/pull/9" in body


def test_render_pr_body_links_issue_when_no_pr() -> None:
    payload = make_payload(pr_url="", issue_url="https://github.com/o/r/issues/9")
    decision = make_decision()
    body = render_pr_body(payload=payload, decision=decision)
    assert "Source issue: https://github.com/o/r/issues/9" in body
    assert "Source PR:" not in body


def test_branch_name_is_deterministic_and_safe() -> None:
    assert branch_name(run_id="019E0E69-198D-7263") == "retrospective/019e0e69-198d-7263"


def test_open_memory_md_pr_invokes_repo_helper_three_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = make_payload()
    decision = make_decision()
    monkeypatch.setattr(
        retrospector_app,
        "fetch_memory_md",
        lambda *, project_slug: EXISTING_MEMORY_MD,
    )
    repo_helper_mock = MagicMock(return_value={"ok": True, "result": {"pr_url": "https://x/pr/9"}})
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", repo_helper_mock)
    pr_url = open_memory_md_pr(payload=payload, decision=decision)
    assert pr_url == "https://x/pr/9"
    ops = [call.kwargs["op"] for call in repo_helper_mock.call_args_list]
    assert ops == ["create_branch", "commit_files", "open_pr"]
    commit_call = repo_helper_mock.call_args_list[1]
    files = commit_call.kwargs["files"]
    assert files[0]["path"] == "docs/MEMORY.md"
    assert "FastAPI + Jinja2 + Alpine.js" in files[0]["content"]


def test_open_memory_md_pr_raises_when_section_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: should never happen because the validator catches it first."""
    payload = make_payload()
    # Bypass the model_validator by constructing via model_construct to
    # represent a hypothetical schema violation.
    decision = RetrospectiveDecision.model_construct(
        has_lesson=True,
        section=None,
        lesson_summary="x",
        memory_md_addition="y",
        rationale="z",
        confidence=0.5,
    )
    monkeypatch.setattr(retrospector_app, "fetch_memory_md", lambda *, project_slug: "")
    with pytest.raises(ValueError, match="section must be set"):
        open_memory_md_pr(payload=payload, decision=decision)
