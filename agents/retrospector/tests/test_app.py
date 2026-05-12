"""Tests for retrospector.app — patch logic + PR-opening flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.runtime import RetrospectorInput
from retrospector import app as retrospector_app
from retrospector.app import (
    branch_name,
    fetch_file,
    open_memory_pr,
    render_agents_md_patch,
    render_patch,
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
        "event_type": "RUN.COMPLETED",
        "project_slug": "ai-dlc",
        "target_repo": "smoketurner/ai-dlc",
        "pr_url": "https://github.com/smoketurner/ai-dlc/pull/42",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
        "correlation_id": "019e0e69-198d-7263-8bfc-7eb9e8ae05df",
    }
    base.update(overrides)
    return RetrospectorInput(**base)


def make_memory_decision(**overrides: Any) -> RetrospectiveDecision:
    base: dict[str, Any] = {
        "has_lesson": True,
        "target_file": "MEMORY.md",
        "section": "conventions",
        "lesson_summary": "Use FastAPI + Jinja2, not React",
        "memory_md_addition": "- **Frontend stack**: FastAPI + Jinja2 + Alpine.js (CDN).",
        "rationale": "Reviewer @jplock rejected SPA in PR #42.",
        "confidence": 0.85,
    }
    base.update(overrides)
    return RetrospectiveDecision(**base)


def make_agents_decision(**overrides: Any) -> RetrospectiveDecision:
    base: dict[str, Any] = {
        "has_lesson": True,
        "target_file": "AGENTS.md",
        "lesson_summary": "Project context: this is a research repo, not production",
        "memory_md_addition": (
            "## Research-only context\n\n"
            "This repository is intended for academic exploration only. "
            "Code in this repo will not be deployed to production environments."
        ),
        "rationale": "Maintainer noted in PR #42 that this is research-only.",
        "confidence": 0.7,
    }
    base.update(overrides)
    return RetrospectiveDecision(**base)


def test_render_patch_appends_under_named_section_for_memory_md() -> None:
    decision = make_memory_decision(memory_md_addition="- Use FastAPI + Jinja2.")
    out = render_patch(existing=EXISTING_MEMORY_MD, decision=decision)
    conventions_idx = out.index("## Conventions")
    decisions_idx = out.index("## Decisions")
    new_bullet_idx = out.index("Use FastAPI + Jinja2")
    assert conventions_idx < new_bullet_idx < decisions_idx
    assert "Use Python 3.14" in out  # existing bullet survives


def test_render_patch_appends_freeform_for_agents_md() -> None:
    existing = "# Project Memory\n\nThis project does X.\n"
    decision = make_agents_decision(
        memory_md_addition="## New section\n\n- New bullet.",
    )
    out = render_patch(existing=existing, decision=decision)
    assert out.startswith("# Project Memory")
    assert "This project does X." in out
    assert "## New section" in out
    assert out.index("## New section") > out.index("This project does X.")


def test_render_agents_md_patch_seeds_default_when_empty() -> None:
    out = render_agents_md_patch(existing="", addition="Some new fact.")
    assert out.startswith("# Project Memory")
    assert "Some new fact." in out


def test_render_pr_title_caps_at_72_chars() -> None:
    decision = make_memory_decision(lesson_summary="A" * 200)
    title = render_pr_title(decision)
    assert len(title) <= 72
    assert title.startswith("retrospective: ")


def test_render_pr_body_includes_target_file_and_section() -> None:
    payload = make_payload(pr_url="https://github.com/o/r/pull/9")
    decision = make_memory_decision()
    body = render_pr_body(payload=payload, decision=decision)
    assert "**Target file:** `MEMORY.md`" in body
    assert "(section `conventions`)" in body
    assert "Source PR: https://github.com/o/r/pull/9" in body


def test_render_pr_body_for_agents_md_omits_section() -> None:
    payload = make_payload(pr_url="https://github.com/o/r/pull/9")
    decision = make_agents_decision()
    body = render_pr_body(payload=payload, decision=decision)
    assert "**Target file:** `AGENTS.md`" in body
    assert "(section " not in body


def test_branch_name_is_deterministic_and_safe() -> None:
    assert branch_name(run_id="019E0E69-198D-7263") == "retrospective/019e0e69-198d-7263"


def test_fetch_file_returns_content_when_repo_helper_says_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        return_value={
            "ok": True,
            "result": {
                "exists": True,
                "content": "# foo\n",
                "sha": "abc",
                "ref": "main",
            },
        },
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    out = fetch_file(repo="o/r", path="AGENTS.md")
    assert out == "# foo\n"
    assert fake.call_args.kwargs == {
        "op": "get_file",
        "repo": "o/r",
        "path": "AGENTS.md",
        "ref": "main",
    }


def test_fetch_file_returns_empty_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        return_value={
            "ok": True,
            "result": {"exists": False, "content": "", "sha": "", "ref": "main"},
        },
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", fake)
    assert fetch_file(repo="o/r", path="MISSING.md") == ""


def test_open_memory_pr_invokes_repo_helper_chain_for_memory_md(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = make_payload()
    decision = make_memory_decision()
    repo_helper_mock = MagicMock(
        side_effect=[
            {  # get_file (root MEMORY.md — exists, ends the candidate probe)
                "ok": True,
                "result": {
                    "exists": True,
                    "content": EXISTING_MEMORY_MD,
                    "sha": "abc",
                    "ref": "main",
                },
            },
            {"ok": True, "result": {"branch": "retrospective/x"}},  # create_branch
            {"ok": True, "result": {"commit_sha": "newsha"}},  # commit_files
            {"ok": True, "result": {"pr_url": "https://x/pr/9", "pr_number": 9}},  # open_pr
        ],
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", repo_helper_mock)
    pr_url = open_memory_pr(payload=payload, decision=decision)
    assert pr_url == "https://x/pr/9"
    ops = [call.kwargs["op"] for call in repo_helper_mock.call_args_list]
    assert ops == ["get_file", "create_branch", "commit_files", "open_pr"]
    commit_call = repo_helper_mock.call_args_list[2]
    files = commit_call.kwargs["files"]
    assert files[0]["path"] == "MEMORY.md"
    assert "FastAPI + Jinja2 + Alpine.js" in files[0]["content"]


def test_open_memory_pr_falls_back_to_docs_memory_md(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``MEMORY.md`` is absent at root, write to ``docs/MEMORY.md``."""
    payload = make_payload()
    decision = make_memory_decision()
    repo_helper_mock = MagicMock(
        side_effect=[
            {"ok": True, "result": {"exists": False, "content": "", "sha": "", "ref": "main"}},
            {
                "ok": True,
                "result": {
                    "exists": True,
                    "content": EXISTING_MEMORY_MD,
                    "sha": "abc",
                    "ref": "main",
                },
            },
            {"ok": True, "result": {"branch": "retrospective/x"}},
            {"ok": True, "result": {"commit_sha": "newsha"}},
            {"ok": True, "result": {"pr_url": "https://x/pr/9", "pr_number": 9}},
        ],
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", repo_helper_mock)
    open_memory_pr(payload=payload, decision=decision)
    commit_call = repo_helper_mock.call_args_list[3]
    files = commit_call.kwargs["files"]
    assert files[0]["path"] == "docs/MEMORY.md"


def test_open_memory_pr_defaults_to_root_when_no_file_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No memory file anywhere — seed a new one at the root."""
    payload = make_payload()
    decision = make_memory_decision()
    repo_helper_mock = MagicMock(
        side_effect=[
            {"ok": True, "result": {"exists": False, "content": "", "sha": "", "ref": "main"}},
            {"ok": True, "result": {"exists": False, "content": "", "sha": "", "ref": "main"}},
            {"ok": True, "result": {"branch": "retrospective/x"}},
            {"ok": True, "result": {"commit_sha": "newsha"}},
            {"ok": True, "result": {"pr_url": "https://x/pr/9", "pr_number": 9}},
        ],
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", repo_helper_mock)
    open_memory_pr(payload=payload, decision=decision)
    commit_call = repo_helper_mock.call_args_list[3]
    files = commit_call.kwargs["files"]
    assert files[0]["path"] == "MEMORY.md"


def test_open_memory_pr_writes_to_agents_md_when_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = make_payload()
    decision = make_agents_decision()
    repo_helper_mock = MagicMock(
        side_effect=[
            {  # get_file
                "ok": True,
                "result": {"exists": False, "content": "", "sha": "", "ref": "main"},
            },
            {"ok": True, "result": {"branch": "retrospective/x"}},
            {"ok": True, "result": {"commit_sha": "newsha"}},
            {"ok": True, "result": {"pr_url": "https://x/pr/9"}},
        ],
    )
    monkeypatch.setattr(retrospector_app, "invoke_repo_helper", repo_helper_mock)
    pr_url = open_memory_pr(payload=payload, decision=decision)
    assert pr_url == "https://x/pr/9"
    get_file_call = repo_helper_mock.call_args_list[0]
    assert get_file_call.kwargs == {
        "op": "get_file",
        "repo": "smoketurner/ai-dlc",
        "path": "AGENTS.md",
        "ref": "main",
    }
    commit_call = repo_helper_mock.call_args_list[2]
    assert commit_call.kwargs["files"][0]["path"] == "AGENTS.md"
    assert "Research-only context" in commit_call.kwargs["files"][0]["content"]


def test_open_memory_pr_raises_when_target_file_missing() -> None:
    payload = make_payload()
    decision = RetrospectiveDecision.model_construct(
        has_lesson=True,
        target_file=None,
        section=None,
        lesson_summary="x",
        memory_md_addition="y",
        rationale="z",
        confidence=0.5,
    )
    with pytest.raises(ValueError, match="target_file must be set"):
        open_memory_pr(payload=payload, decision=decision)
