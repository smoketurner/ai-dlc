"""Tests for the synthetic spec renderer.

The renderers must produce markdown the Implementer's :mod:`tasks` parser
can read — that's the whole point of the synthetic-spec branch.
"""

from __future__ import annotations

import pytest

from implementer.tasks import parse_tasks  # cross-package: contract test against the parser
from triage_dispatcher import synthesize


@pytest.mark.parametrize("kind", ["bug_fix", "upgrade", "docs"])
def test_render_tasks_is_parseable_by_implementer(kind: synthesize.WorkflowKind) -> None:
    md = synthesize.render_tasks(kind=kind, issue_url="https://github.com/o/r/issues/1")

    rows = parse_tasks(md)

    assert len(rows) == 1
    assert rows[0].id == "T-001"
    assert rows[0].done is False
    assert rows[0].implements == ["AC-001"]
    assert rows[0].done_when is not None


def test_render_requirements_includes_issue_body() -> None:
    md = synthesize.render_requirements(
        issue_title="App crashes on /healthz",
        issue_body="Steps:\n1. hit /healthz\n2. observe 500",
        issue_url="https://github.com/o/r/issues/9",
    )

    assert "App crashes on /healthz" in md
    assert "Steps:" in md
    assert "https://github.com/o/r/issues/9" in md
    assert "AC-001" in md


def test_render_design_per_kind_differs() -> None:
    bug = synthesize.render_design(kind="bug_fix", issue_url="https://github.com/o/r/issues/1")
    docs = synthesize.render_design(kind="docs", issue_url="https://github.com/o/r/issues/1")

    assert "reproduce" in bug.lower()
    assert "markdown-only" in docs.lower()
    assert bug != docs


def test_render_requirements_handles_empty_body() -> None:
    md = synthesize.render_requirements(
        issue_title="Need new endpoint",
        issue_body="",
        issue_url="https://github.com/o/r/issues/1",
    )

    assert "(no body provided)" in md
