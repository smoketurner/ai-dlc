"""Tests for ``agent_memory_preamble`` and ``render_memory_preamble``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.agentcore_memory import MemoryRecord
from common.errors import AgentCoreMemoryError
from common.memory import (
    agent_memory_preamble,
    agent_skills_preamble,
    memory_client,
    parse_skill_frontmatter,
    render_memory_preamble,
)


@pytest.fixture(autouse=True)
def reset_client_cache() -> Iterator[None]:
    """The process-cached client must not leak between tests."""
    memory_client.cache_clear()
    yield
    memory_client.cache_clear()


def test_render_empty_records_returns_empty() -> None:
    assert render_memory_preamble([]) == ""


def test_render_records_emits_markdown_block() -> None:
    records = [
        MemoryRecord(
            record_id="r1",
            namespace="/projects/demo/facts",
            content="Use FastAPI + Jinja2 for the dashboard.",
            score=0.9,
        ),
        MemoryRecord(
            record_id="r2",
            namespace="/projects/demo/facts",
            content="ECS Fargate behind an ALB; not App Runner.",
            score=0.85,
        ),
    ]

    out = render_memory_preamble(records)

    assert out.startswith("## Recent project context")
    assert "FastAPI + Jinja2" in out
    assert "ECS Fargate" in out
    assert out.endswith("---\n")


def test_render_skips_blank_content() -> None:
    records = [
        MemoryRecord(record_id="r1", namespace="ns", content="real fact", score=0.9),
        MemoryRecord(record_id="r2", namespace="ns", content="   ", score=0.8),
    ]
    out = render_memory_preamble(records)
    assert "real fact" in out
    assert out.count("- ") == 1


def test_preamble_returns_empty_when_memory_id_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIDLC_MEMORY_ID", raising=False)

    out = agent_memory_preamble(project_slug="demo", query="anything")

    assert out == ""


def test_preamble_passes_namespace_and_query_to_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDLC_MEMORY_ID", "mem-1")
    fake_client = MagicMock()
    fake_client.retrieve_memory_records.return_value = {
        "memoryRecordSummaries": [
            {"memoryRecordId": "r1", "content": {"text": "fact one"}, "score": 0.9},
        ],
    }

    out = agent_memory_preamble(
        project_slug="demo",
        query="add /healthz endpoint",
        top_k=5,
        client=fake_client,
    )

    fake_client.retrieve_memory_records.assert_called_once_with(
        memoryId="mem-1",
        namespace="/projects/demo/facts",
        searchCriteria={"searchQuery": "add /healthz endpoint", "topK": 5},
    )
    assert "fact one" in out
    assert "## Recent project context" in out


def test_preamble_returns_empty_on_retrieval_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory-store outages must not gate the agent."""
    monkeypatch.setenv("AIDLC_MEMORY_ID", "mem-1")
    fake_client = MagicMock()
    fake_client.retrieve_memory_records.side_effect = (
        # The wrapper converts boto exceptions into AgentCoreMemoryError —
        # but here we simulate the wrapper-level failure directly to keep
        # the test focused on the catch-and-return-empty contract.
        _make_memory_error()
    )

    out = agent_memory_preamble(
        project_slug="demo",
        query="anything",
        client=fake_client,
    )

    assert out == ""


def _make_memory_error() -> AgentCoreMemoryError:
    return AgentCoreMemoryError("boom", memory_id="mem-1", namespace="/projects/demo/facts")


def test_preamble_returns_empty_when_no_records_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDLC_MEMORY_ID", "mem-1")
    fake_client = MagicMock()
    fake_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}

    out = agent_memory_preamble(
        project_slug="demo",
        query="anything",
        client=fake_client,
    )

    assert out == ""


# --- skills preamble (agentskills.io folder layout) -----------------------


def test_parse_skill_frontmatter_extracts_name_and_description() -> None:
    content = (
        "---\nname: handle-pagination\ndescription: Use src/api/pagination.ts.\n---\n\n## Body\n"
    )
    assert parse_skill_frontmatter(content) == (
        "handle-pagination",
        "Use src/api/pagination.ts.",
    )


def test_parse_skill_frontmatter_returns_none_without_frontmatter() -> None:
    assert parse_skill_frontmatter("# Just a heading\n") is None


def test_parse_skill_frontmatter_returns_none_when_required_keys_missing() -> None:
    assert parse_skill_frontmatter("---\nname: foo\n---\n") is None
    assert parse_skill_frontmatter("---\ndescription: x\n---\n") is None


def test_skills_preamble_lists_canonical_folder_skills(tmp_path: Path) -> None:
    """Walks ``<dir>/<slug>/SKILL.md`` (folder layout) under repo root."""
    repo = tmp_path / "repo"
    pagination = repo / ".aidlc" / "skills" / "handle-pagination"
    pagination.mkdir(parents=True)
    (pagination / "SKILL.md").write_text(
        "---\nname: handle-pagination\ndescription: Use src/api/pagination.ts.\n---\n\n## Body\n",
        encoding="utf-8",
    )

    out = agent_skills_preamble(fs_root=tmp_path)

    assert "## Available skills" in out
    assert "handle-pagination" in out
    assert "Use src/api/pagination.ts" in out


def test_skills_preamble_returns_empty_when_no_skills(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    assert agent_skills_preamble(fs_root=tmp_path) == ""


def test_skills_preamble_silently_skips_unparseable_skill(tmp_path: Path) -> None:
    """A SKILL.md with no frontmatter is skipped, not crashed on."""
    repo = tmp_path / "repo"
    bad = repo / ".aidlc" / "skills" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("Just a body, no frontmatter.\n", encoding="utf-8")
    good = repo / ".aidlc" / "skills" / "good-one"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(
        "---\nname: good-one\ndescription: Works.\n---\n\nBody.\n",
        encoding="utf-8",
    )

    out = agent_skills_preamble(fs_root=tmp_path)

    assert "good-one" in out
    assert "broken" not in out
